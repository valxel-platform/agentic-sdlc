"""How far a gate's approval can be predicted, per product, from its own history (T-81).

At G1-G4, the tool asks: of this product's earlier decisions at this gate, for the same kind of
change, under the same agent config, how many did people approve first time and keep (nothing
sent back later from build, graph/gates.reopen)? It reports that as a cautious lower bound, not
the raw rate: 38 of 40 is 95%, but its lower bound is about 86%, and a small run of luck can't
look confident.

  kind of change   the workflow profile, the product's data class, and whether other lanes'
                   repos are touched. Coarse on purpose: finer kinds take longer to say anything.
  config           the agents' config version (pilot.config_version). A new model or prompt
                   starts the count over: history under another config says little about this one.
  this feature     the score is about the category; the feature must also pass its own checks:
                   its phase passed first try with the critic's checklist met in full.

  high     the lower bound is at least `high` (config/confidence.yaml), with at least
           `min_samples` decisions, and this feature passes its checks
  medium   the lower bound is at least `medium`
  low      below that
  unknown  too few decisions to say

This is steps 1 and 2 of three: the score is shown to the approver and kept with the decision, so
the tool learns how often "high" was right. Nothing is skipped. Skipping G2 or G3 when confidence is
high (step 3, T-82) waits for that record, needs the product to opt in, and keeps a random share of
those decisions with people. G1, G4 and G6 are never automated (VX-1): there a high score only
says the reading can be quick.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

GATES = ("g1", "g2", "g3", "g4")
NEVER_AUTOMATED = ("g1", "g4", "g6")  # VX-1: intent, architecture and production stay with people
REGULATED = ("phi", "payments")


@dataclass(frozen=True)
class Settings:
    min_samples: int = 20
    high: float = 0.95
    medium: float = 0.8
    z: float = 1.645  # one-sided 95%

    @classmethod
    def load(cls, config_dir: Path | str) -> Settings:
        from .config import config_file

        path = config_file(config_dir, "confidence.yaml")
        raw = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        return cls(int(raw.get("min_samples", 20)), float(raw.get("high", 0.95)), float(raw.get("medium", 0.8)),
                   float(raw.get("z", 1.645)))


def lower_bound(held: int, n: int, z: float = 1.645) -> float:
    """Wilson score lower bound of a proportion: what the rate is at least, with this many samples."""
    if n <= 0:
        return 0.0
    p = held / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(max(0.0, (centre - spread) / (1 + z * z / n)), 4)


def kind_of(state: dict, data_class: str) -> str:
    return (f"{state.get('profile') or 'feature'}/{data_class or 'general'}/"
            f"{'cross-lane' if state.get('lanes_touched') else 'own-lane'}")


def feature_checks(state: dict, phase: str) -> list[str]:
    """Why this feature's own work isn't as clean as the ones the score counts; [] when it is."""
    why = []
    tries = int((state.get("loop_count") or {}).get(phase, 0))
    if tries > 1:
        why.append(f"{phase} needed {tries} tries")
    critic = next((e.get("critic") for e in reversed(state.get("events") or [])
                   if e.get("node") == phase and e.get("kind") == "passed"), None) or {}
    if not critic.get("passed"):
        why.append("the critic's checklist didn't pass")
    elif any(not i.get("met") for i in critic.get("items") or []):
        why.append("checklist items not met: " + ", ".join(str(i.get("id")) for i in critic["items"] if not i.get("met")))
    if (state.get("feedback") or {}).get(phase):
        why.append("people asked for changes to it before")
    return why


# --------------------------------------------------------------------------- the record


class OutcomeStore(Protocol):
    def add(self, row: dict) -> None: ...
    def missed(self, feature_id: str, gates: tuple[str, ...], why: str) -> None: ...
    def rows(self, product_id: str, gate: str | None = None) -> list[dict]: ...


class InMemoryOutcomeStore:
    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, row: dict) -> None:
        with self._lock:
            if any(r["feature_id"] == row["feature_id"] and r["gate"] == row["gate"] for r in self._rows):
                return  # a gate's first decision only
            self._rows.append({"missed": "", **row})

    def missed(self, feature_id, gates, why) -> None:
        with self._lock:
            for r in self._rows:
                if r["feature_id"] == feature_id and r["gate"] in gates and r["decision"] == "approve" and not r["missed"]:
                    r["missed"] = why

    def rows(self, product_id, gate=None) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows if r["product_id"] == product_id and (gate is None or r["gate"] == gate)]


class PostgresOutcomeStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS valxel_gate_outcomes (
             product_id TEXT NOT NULL, feature_id TEXT NOT NULL, gate TEXT NOT NULL, kind TEXT NOT NULL,
             config TEXT NOT NULL DEFAULT '', decision TEXT NOT NULL, missed TEXT NOT NULL DEFAULT '',
             predicted JSONB NOT NULL DEFAULT '{}', at TIMESTAMPTZ NOT NULL DEFAULT now(),
             PRIMARY KEY (feature_id, gate))""",
        "CREATE INDEX IF NOT EXISTS valxel_gate_outcomes_product ON valxel_gate_outcomes (product_id, gate)",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def add(self, row: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO valxel_gate_outcomes (product_id, feature_id, gate, kind, config, decision, predicted) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (row["product_id"], row["feature_id"], row["gate"], row["kind"], row.get("config", ""),
                 row["decision"], json.dumps(row.get("predicted") or {})))

    def missed(self, feature_id, gates, why) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE valxel_gate_outcomes SET missed = %s WHERE feature_id = %s AND gate = ANY(%s) "
                         "AND decision = 'approve' AND missed = ''", (why, feature_id, list(gates)))

    ALL = "SELECT * FROM valxel_gate_outcomes WHERE product_id = %s ORDER BY at"
    ONE_GATE = "SELECT * FROM valxel_gate_outcomes WHERE product_id = %s AND gate = %s ORDER BY at"

    def rows(self, product_id, gate=None) -> list[dict]:
        with self.pool.connection() as conn:
            got = (conn.execute(self.ONE_GATE, (product_id, gate)) if gate
                   else conn.execute(self.ALL, (product_id,))).fetchall()
        return [{**dict(r), "at": r["at"].isoformat()} for r in got]


# --------------------------------------------------------------------------- the score


def held(row: dict) -> bool:
    return row["decision"] == "approve" and not row.get("missed")


def score(rows: list[dict], gate: str, kind: str, config: str, cfg: Settings) -> dict:
    """The category's record: decisions, how many held, the rate, its lower bound, and the level
    (before this feature's own checks)."""
    mine = [r for r in rows if r["gate"] == gate and r["kind"] == kind and r.get("config", "") == config]
    n, k = len(mine), sum(1 for r in mine if held(r))
    lb = lower_bound(k, n, cfg.z)
    level = ("unknown" if n < cfg.min_samples else "high" if lb >= cfg.high else "medium" if lb >= cfg.medium
             else "low")
    return {"gate": gate, "kind": kind, "config": config, "decisions": n, "held": k,
            "rate": round(k / n, 4) if n else None, "lower_bound": lb, "level": level}


def assess(services: Any, state: dict, gate: str, phase: str) -> dict | None:
    """What the approver sees at the gate, and what's kept with the decision."""
    if gate not in GATES or getattr(services, "gate_outcomes", None) is None:
        return None
    from .config import config_version

    snap = services.feature_snapshot(state)
    data_class = snap.product.data_class
    cfg = Settings.load(services.settings.config_dir)
    s = score(services.gate_outcomes.rows(state["product_id"], gate), gate, kind_of(state, data_class),
              config_version(services.settings), cfg)
    checks = feature_checks(state, phase)
    level = s["level"] if not checks or s["level"] in ("unknown", "low") else "medium"
    out = {**s, "level": level, "category_level": s["level"], "checks": checks,
           "skippable": False,  # step 3 (T-82); never at G1, G4, G6 nor for PHI or payments
           "says": says(gate, phase, {**s, "level": level}, checks)}
    from .safe_mode import active

    if active(services, state["product_id"]):  # the history was made by agents now in doubt: no quick reads
        out["quick_read"] = False
        out["says"] = (out["says"].replace(" You still decide here, but this one can be a quick read.", "")
                       .replace(" A candidate for a quick approval.", "")
                       + " Safe mode is on: read it in full whatever the history says.")
    else:
        out["quick_read"] = level == "high"
    return out


def says(gate: str, phase: str, s: dict, checks: list[str]) -> str:
    what = {"discovery": "briefs", "spec": "specs", "design": "UX designs", "architecture": "architectures"}.get(phase, phase)
    if s["level"] == "unknown":
        return (f"Not enough history yet: {s['decisions']} earlier decisions on {what} like this one under the "
                "current agent config.")
    record = (f"{s['held']} of {s['decisions']} {what} like this were approved first time and kept "
              f"(at least {s['lower_bound']:.0%})")
    if s["level"] == "high":
        tail = (" You still decide here, but this one can be a quick read." if gate in NEVER_AUTOMATED
                else " A candidate for a quick approval.")
        return f"High confidence: {record}.{tail}"
    if checks:
        return f"{s['level'].capitalize()} confidence: {record}, but this one differs: {'; '.join(checks)}."
    return f"{s['level'].capitalize()} confidence: {record}."


def record(services: Any, state: dict, gate: str, decision: str, predicted: dict | None) -> None:
    """Keep the gate's first decision for this feature, with what was predicted."""
    store = getattr(services, "gate_outcomes", None)
    if store is None or gate not in GATES or not predicted:
        return
    store.add({"product_id": state["product_id"], "feature_id": state["feature_id"], "gate": gate,
               "kind": predicted["kind"], "config": predicted["config"], "decision": decision,
               "predicted": {k: predicted[k] for k in ("level", "category_level", "lower_bound", "decisions")}})


def calibration(rows: list[dict]) -> dict:
    """How often each predicted level held up: the record step 3 is decided on."""
    out: dict[str, dict] = {}
    for r in rows:
        level = (r.get("predicted") or {}).get("level") or "unknown"
        c = out.setdefault(level, {"decisions": 0, "held": 0})
        c["decisions"] += 1
        c["held"] += held(r)
    return {lvl: {**c, "rate": round(c["held"] / c["decisions"], 4)} for lvl, c in out.items()}


def report(store: OutcomeStore, product_id: str, cfg: Settings, config: str) -> dict:
    """Per gate and kind of change under the current config, and how the predictions held up."""
    rows = store.rows(product_id)
    cats = sorted({(r["gate"], r["kind"]) for r in rows if r.get("config", "") == config})
    return {"product_id": product_id, "config": config,
            "categories": [score(rows, g, k, config, cfg) for g, k in cats],
            "calibration": calibration(rows),
            "settings": {"min_samples": cfg.min_samples, "high": cfg.high, "medium": cfg.medium}}
