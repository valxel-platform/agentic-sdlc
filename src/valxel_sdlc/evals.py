"""Evals of the tool's own agents: eval sets from real work, replayed under a candidate config,
compared with the config in use, before a model, prompt or setting change goes live.

**Cases come from history.** Every time a document phase (discovery, spec, design, architecture,
plan) finishes, the tool keeps an eval case: what the agent was given (the phase's context: the
request, the parent documents, the product's knowledge), what it wrote, and the critic's verdict.
The gate that follows labels it with what people decided (approved first time, sent back and why,
taken over), and the version people finally approved becomes the case's reference.

**A run replays cases** under a config (config/models.yaml and the rest), with its own ledger, a
stand-in GitHub and OpenChoreo, so nothing real is touched and no feature's cost moves. For each
case it measures:

  passed          the new document passes the deterministic checks and the checklist critic
  must_met        the share of the checklist's must-have items met
  critic_agrees   the config's critic, reading the document people decided on, agrees with them
                  (passes what people approved first time, fails what they sent back)
  pairwise        optional: a judge compares the new document with the one people approved
  cost, time      dollars and seconds

**A comparison** runs the same cases under the config in use and the candidate and fails when the
candidate's pass rate, must-have rate, critic agreement or pairwise result drop by more than
`max_drop`, or (if set) its cost rises by more than `max_cost_increase`. `valxel eval run` does this
from the command line; a pull request to the config repo does it through the tool (api/app.py),
which posts the result as a commit status.

Cases hold product documents. Each run sends them only to the providers the case's data class
allows (the gateway enforces it), and only cases of the data classes asked for are exported or
sent to Langfuse with their content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

DOC_PHASES = ("discovery", "spec", "design", "architecture", "plan")
# What a replay doesn't need from the phase's context, or must not inherit from the run it came from.
_DROP = ("feedback", "prior_findings", "simulate_faults", "budget_remaining_usd", "pilot", "deployments", "quality")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EvalCase:
    id: str
    product_id: str
    feature_id: str
    phase: str
    data_class: str
    at: str
    config_version: str
    ctx: dict
    output: str
    status: str  # passed | escalated
    critic: dict = field(default_factory=dict)
    # What people decided: {"decision", "reason", "feedback", "first_time"}; None until a gate decides.
    label: dict | None = None
    reference: str = ""  # the version people approved (artifact text), once there is one

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "EvalCase":
        return EvalCase(**{k: d.get(k) for k in EvalCase.__dataclass_fields__ if k in d})


def case_from(state: dict, phase: str, ctx: dict, out: dict, *, config_version: str = "") -> EvalCase:
    """The eval case a finished document phase leaves behind."""
    n = sum(1 for e in state.get("events") or [] if e.get("node") == phase and e.get("kind") in ("passed", "escalated"))
    keep = json.loads(json.dumps({k: v for k, v in ctx.items() if k not in _DROP}, default=str))
    from .agents import PHASE_SPECS

    content = (out.get("draft") or {}).get("content", "")
    # A phase without a gate (the plan) has no person's decision: the next phase takes what passed.
    no_gate = not getattr(PHASE_SPECS[phase], "gate", None) and out.get("status") == "passed"
    return EvalCase(id=f"{state['feature_id']}:{phase}:{n + 1}", product_id=state["product_id"],
                    feature_id=state["feature_id"], phase=phase, data_class=ctx.get("data_class", ""),
                    at=now(), config_version=config_version, ctx=keep,
                    output=content, status=out.get("status", ""), critic=dict(out.get("critic") or {}),
                    label={"decision": "no_gate", "first_time": None} if no_gate else None,
                    reference=content if no_gate else "")


# ---------------------------------------------------------------------------------------- stores

class EvalStore(Protocol):
    def capture(self, case: EvalCase) -> None: ...
    def label(self, feature_id: str, phase: str, label: dict, reference: str = "") -> None: ...
    def cases(self, *, product_id: str | None = None, phases: tuple[str, ...] = (),
              data_classes: tuple[str, ...] = (), labeled: bool = True, limit_per_phase: int | None = None) -> list[EvalCase]: ...


def _select(cases, product_id, phases, data_classes, labeled, limit_per_phase):
    out, seen = [], {}
    for c in sorted(cases, key=lambda c: c.at, reverse=True):  # newest first
        if (product_id and c.product_id != product_id) or (phases and c.phase not in phases) \
                or (data_classes and c.data_class not in data_classes) or (labeled and not c.label):
            continue
        if limit_per_phase is not None and seen.get(c.phase, 0) >= limit_per_phase:
            continue
        seen[c.phase] = seen.get(c.phase, 0) + 1
        out.append(c)
    return out


def _apply_label(cases: list[EvalCase], label: dict, reference: str) -> None:
    """The newest unlabeled case gets the decision; an approval is the reference for all of them."""
    open_ = [c for c in cases if not c.label]
    if open_:
        first = not any(c.label for c in cases)
        open_[-1].label = {**label, "first_time": first and label.get("decision") == "approve"}
    if label.get("decision") == "approve" and reference:
        for c in cases:
            c.reference = c.reference or reference


class InMemoryEvalStore:
    def __init__(self) -> None:
        self._cases: dict[str, EvalCase] = {}
        self._lock = threading.Lock()

    def capture(self, case: EvalCase) -> None:
        with self._lock:
            self._cases[case.id] = case

    def label(self, feature_id: str, phase: str, label: dict, reference: str = "") -> None:
        with self._lock:
            mine = sorted((c for c in self._cases.values() if c.feature_id == feature_id and c.phase == phase),
                          key=lambda c: c.at)
            _apply_label(mine, label, reference)

    def cases(self, *, product_id=None, phases=(), data_classes=(), labeled=True, limit_per_phase=None):
        with self._lock:
            return _select(list(self._cases.values()), product_id, phases, data_classes, labeled, limit_per_phase)


class PostgresEvalStore:
    DDL = ("""CREATE TABLE IF NOT EXISTS valxel_eval_cases (
                id TEXT PRIMARY KEY, product_id TEXT NOT NULL, feature_id TEXT NOT NULL, phase TEXT NOT NULL,
                data_class TEXT NOT NULL, at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL)""",
           "CREATE INDEX IF NOT EXISTS valxel_eval_cases_product ON valxel_eval_cases (product_id, phase)",
           "CREATE INDEX IF NOT EXISTS valxel_eval_cases_feature ON valxel_eval_cases (feature_id, phase)")

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def _put(self, conn, c: EvalCase) -> None:
        conn.execute("INSERT INTO valxel_eval_cases (id, product_id, feature_id, phase, data_class, at, data) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                     (c.id, c.product_id, c.feature_id, c.phase, c.data_class, c.at, json.dumps(c.as_dict(), default=str)))

    def capture(self, case: EvalCase) -> None:
        with self.pool.connection() as conn:
            self._put(conn, case)

    def label(self, feature_id: str, phase: str, label: dict, reference: str = "") -> None:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM valxel_eval_cases WHERE feature_id = %s AND phase = %s ORDER BY at",
                                (feature_id, phase)).fetchall()
            mine = [EvalCase.from_dict(dict(r["data"])) for r in rows]
            _apply_label(mine, label, reference)
            for c in mine:
                self._put(conn, c)

    def cases(self, *, product_id=None, phases=(), data_classes=(), labeled=True, limit_per_phase=None):
        sql, args = "SELECT data FROM valxel_eval_cases", []
        if product_id:
            sql, args = sql + " WHERE product_id = %s", [product_id]
        with self.pool.connection() as conn:
            rows = conn.execute(sql + " ORDER BY at DESC LIMIT 5000", args).fetchall()
        return _select([EvalCase.from_dict(dict(r["data"])) for r in rows], product_id, phases, data_classes,
                       labeled, limit_per_phase)


def load_jsonl(path: str | Path) -> list[EvalCase]:
    return [EvalCase.from_dict(json.loads(line)) for line in Path(path).read_text().splitlines() if line.strip()]


def dump_jsonl(cases: list[EvalCase], path: str | Path) -> None:
    Path(path).write_text("".join(json.dumps(c.as_dict(), default=str) + "\n" for c in cases))


# ---------------------------------------------------------------------------------------- replay

PAIRWISE_SCHEMA = {"type": "object", "required": ["winner"], "additionalProperties": False,
                   "properties": {"winner": {"enum": ["a", "b", "tie"]}, "reason": {"type": "string"}}}


def services_for(base_settings, config_dir: str | Path | None = None):
    """Services for a run: the config in `config_dir` (the one in use when None), the same
    governance, its own ledger and stand-ins for everything with side effects."""
    from .config import load_settings
    from .gateway import InMemoryLedger
    from .integrations import StubGitHub, StubOpenChoreo, StubPackageRegistry
    from .services import build_services, use_own_plugins

    settings = load_settings(mode=base_settings.mode, database_url="", config_dir=config_dir or base_settings.config_dir,
                             artifact_dir=tempfile.mkdtemp(prefix="valxel-eval-"),
                             governance_source=base_settings.governance_source,
                             governance_root=base_settings.governance_root)
    use_own_plugins(settings)  # a candidate that moves the plugin pin runs with the plugins it pins
    return build_services(settings, ledger=InMemoryLedger(), github=StubGitHub(), openchoreo=StubOpenChoreo(),
                          packages=StubPackageRegistry(), diagrams={}, trackers={})


def people_approved(case: EvalCase) -> bool | None:
    """Whether people accepted this document as the agents wrote it; None without a person's decision."""
    lab = case.label or {}
    d = lab.get("decision")
    if d in (None, "", "no_gate"):
        return None
    if d == "escalation_retry":
        return False
    return d in ("approve", "escalation_resolved") and not lab.get("taken_over")


def _pairwise_order(case_id: str) -> bool:
    """True: the new document is A. Fixed per case, so positions don't favour one config."""
    return int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 2 == 0


def replay(services, case: EvalCase, *, pairwise: bool = False) -> dict:
    from .agents import PHASE_SPECS, templates
    from .evaluators import run_checks
    from .evaluators.critic import critique
    from .graph.phase import call_agent, document_tail

    spec = PHASE_SPECS[case.phase]
    ctx = {**case.ctx, "simulate_faults": {}, "feedback": None, "prior_findings": []}
    started = time.monotonic()
    before = len(services.ledger.records())
    res = call_agent(services, role=spec.role, phase=case.phase, ctx=ctx, skills=spec.skills,
                     tail=document_tail(case.phase, ctx), stub_text=templates.PHASE_TEMPLATES[case.phase](ctx, False))
    content = res.text
    findings = run_checks(case.phase, content, ctx)
    critic_passed, must_met = False, 0.0
    if not findings:
        v = critique(services, phase=case.phase, rubric_skills=spec.critic_skills, content=content, ctx=ctx)
        critic_passed = v.passed
        musts = [i for i in v.items if i.get("must", True)]
        must_met = sum(1 for i in musts if i.get("met")) / len(musts) if musts else 1.0
    # The config's critic on the document people decided on: does it agree with them?
    agrees = None
    expected = people_approved(case)
    if expected is not None and case.output and not run_checks(case.phase, case.output, ctx):
        ov = critique(services, phase=case.phase, rubric_skills=spec.critic_skills, content=case.output, ctx=ctx)
        agrees = ov.passed == expected
    duel = None
    if pairwise and case.reference:
        duel = _pairwise(services, case, content, ctx)
    rows = services.ledger.records()[before:]
    return {"case_id": case.id, "phase": case.phase, "data_class": case.data_class,
            "passed": not findings and critic_passed, "findings": len(findings), "must_met": round(must_met, 4),
            "critic_agrees": agrees, "pairwise": duel,
            "cost_usd": round(sum(r.est_cost_usd for r in rows), 6),
            "duration_s": round(time.monotonic() - started, 3),
            "trace_ids": sorted({r.trace_id for r in rows if r.trace_id})}


def _pairwise(services, case: EvalCase, content: str, ctx: dict) -> str:
    """win, tie or loss for the new document against the version people approved."""
    from .agents import untrusted
    from .graph.phase import call_agent

    new_is_a = _pairwise_order(case.id)
    a, b = (content, case.reference) if new_is_a else (case.reference, content)
    tail = (f"Two versions of the same {case.phase} document for the same request. Which better meets the parent "
            "documents, the format contract and the phase's checklist? Judge substance, not length. Reply in JSON: "
            '{"winner": "a" | "b" | "tie", "reason": "..."}.\n\n'
            + untrusted.wrap("version-a", a) + "\n\n" + untrusted.wrap("version-b", b))
    res = call_agent(services, role="critic", phase=case.phase, ctx=ctx, skills=(), tail=tail, max_tokens=512,
                     stub_text=json.dumps({"winner": "tie", "reason": "stub"}), schema=PAIRWISE_SCHEMA)
    m = re.search(r"\{.*\}", res.text or "", re.S)
    try:
        winner = json.loads(m.group(0)).get("winner", "tie") if m else "tie"
    except ValueError:
        winner = "tie"
    if winner == "tie":
        return "tie"
    return "win" if (winner == "a") == new_is_a else "loss"


def run(services, cases: list[EvalCase], *, pairwise: bool = False, budget_usd: float | None = None) -> dict:
    """Replay every case (until the budget is spent); per-case results and a summary."""
    results, spent = [], 0.0
    for c in cases:
        if budget_usd is not None and spent >= budget_usd:
            log.warning("eval budget of $%.2f spent: %d cases not run", budget_usd, len(cases) - len(results))
            break
        try:
            r = replay(services, c, pairwise=pairwise)
        except Exception as exc:  # one case failing to run is a result, not the end of the run
            r = {"case_id": c.id, "phase": c.phase, "data_class": c.data_class, "error": f"{type(exc).__name__}: {exc}",
                 "passed": False, "findings": 0, "must_met": 0.0, "critic_agrees": None, "pairwise": None,
                 "cost_usd": 0.0, "duration_s": 0.0, "trace_ids": []}
        spent += r["cost_usd"]
        results.append(r)
    return {"results": results, "summary": summarize(results)}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(results: list[dict]) -> dict:
    def block(rs):
        duels = [r["pairwise"] for r in rs if r.get("pairwise")]
        agree = [r["critic_agrees"] for r in rs if r.get("critic_agrees") is not None]
        return {"cases": len(rs), "errors": sum(1 for r in rs if r.get("error")),
                "pass_rate": _mean([1.0 if r["passed"] else 0.0 for r in rs]),
                "must_met_rate": _mean([r["must_met"] for r in rs]),
                "critic_agreement": _mean([1.0 if a else 0.0 for a in agree]),
                "pairwise": {k: duels.count(k) for k in ("win", "tie", "loss")} if duels else None,
                "pairwise_loss_rate": _mean([1.0 if d == "loss" else 0.0 for d in duels]),
                "cost_usd": round(sum(r["cost_usd"] for r in rs), 4),
                "mean_duration_s": _mean([r["duration_s"] for r in rs])}

    phases = sorted({r["phase"] for r in results})
    return {"all": block(results), "phases": {p: block([r for r in results if r["phase"] == p]) for p in phases}}


METRICS = ("pass_rate", "must_met_rate", "critic_agreement")


def compare(base: dict, cand: dict, *, max_drop: float = 0.05, max_cost_increase: float | None = None) -> dict:
    """The candidate against the config in use, on the same cases: ok, or what regressed."""
    regressions, rows = [], {}
    b, c = base["summary"], cand["summary"]
    for scope in ["all", *sorted(c["phases"])]:
        bs = b["all"] if scope == "all" else b["phases"].get(scope)
        cs = c["all"] if scope == "all" else c["phases"].get(scope)
        if not bs or not cs:
            continue
        row = {}
        for m in METRICS:
            if bs.get(m) is None or cs.get(m) is None:
                continue
            delta = round(cs[m] - bs[m], 4)
            row[m] = {"base": bs[m], "candidate": cs[m], "delta": delta}
            if delta < -max_drop:
                regressions.append(f"{scope}: {m.replace('_', ' ')} fell from {bs[m]:.0%} to {cs[m]:.0%}")
        if cs.get("pairwise_loss_rate") is not None and cs["pairwise_loss_rate"] > max_drop + (bs.get("pairwise_loss_rate") or 0.0):
            regressions.append(f"{scope}: the candidate's documents lost to the approved ones in "
                               f"{cs['pairwise_loss_rate']:.0%} of comparisons")
        if bs["cost_usd"]:
            rise = (cs["cost_usd"] - bs["cost_usd"]) / bs["cost_usd"]
            row["cost_usd"] = {"base": bs["cost_usd"], "candidate": cs["cost_usd"], "change": round(rise, 4)}
            if max_cost_increase is not None and rise > max_cost_increase and scope == "all":
                regressions.append(f"cost rose {rise:.0%} (limit {max_cost_increase:.0%})")
        if cs.get("errors"):
            regressions.append(f"{scope}: {cs['errors']} cases didn't run")
        rows[scope] = row
    return {"ok": not regressions, "regressions": regressions, "metrics": rows,
            "cases": c["all"]["cases"], "max_drop": max_drop}


def headline(cmp: dict) -> str:
    """One line for a commit status (140 characters)."""
    if cmp["ok"]:
        m = cmp["metrics"].get("all", {})
        bits = [f"{k.split('_')[0]} {v['candidate']:.0%}" for k, v in m.items() if k in METRICS]
        return (f"{cmp['cases']} cases, no regression: " + ", ".join(bits))[:140]
    return (f"{len(cmp['regressions'])} regressions: " + "; ".join(cmp["regressions"]))[:140]


def markdown(cmp: dict, base: dict, cand: dict) -> str:
    lines = [f"# Eval run: {'no regression' if cmp['ok'] else 'regressions'}", "",
             f"{cmp['cases']} cases, max drop {cmp['max_drop']:.0%}.", ""]
    lines += [f"- {r}" for r in cmp["regressions"]] or ["- None."]
    lines += ["", "| Scope | Pass rate | Must-haves met | Critic agrees with people | Cost |", "| --- | --- | --- | --- | --- |"]
    for scope, row in cmp["metrics"].items():
        def cell(m):
            v = row.get(m)
            return f"{v['base']:.0%} → {v['candidate']:.0%}" if v else "n/a"
        cost = row.get("cost_usd")
        lines.append(f"| {scope} | {cell('pass_rate')} | {cell('must_met_rate')} | {cell('critic_agreement')} | "
                     + (f"${cost['base']:.2f} → ${cost['candidate']:.2f}" if cost else "n/a") + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------- config at a git ref

def config_dir_at(github, repo: str, ref: str, base_dir: str | Path, files: tuple[str, ...] | None = None) -> Path:
    """A copy of the config in use with the files the config repo has at `ref` laid over it."""
    target = Path(tempfile.mkdtemp(prefix="valxel-config-"))
    shutil.copytree(base_dir, target, dirs_exist_ok=True)
    for f in files or tuple(p.name for p in Path(base_dir).glob("*.yaml")):
        text = github.get_file(repo=repo, path=f"config/{f}", ref=ref)
        if text is not None:
            (target / f).write_text(text)
    return target


# ---------------------------------------------------------------------------------------- Langfuse

class Langfuse:
    """Datasets, run items and scores through Langfuse's public API (best effort)."""

    def __init__(self, url: str, public_key: str, secret_key: str, opener=None) -> None:
        import base64
        from urllib.request import build_opener

        self.url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.opener = opener or build_opener()

    @staticmethod
    def from_env(env=None) -> "Langfuse | None":

        from .secret_ring import environ

        env = env if env is not None else environ()
        url, pk, sk = env.get("VALXEL_LANGFUSE_URL"), env.get("VALXEL_LANGFUSE_PUBLIC_KEY"), env.get("VALXEL_LANGFUSE_SECRET_KEY")
        return Langfuse(url, pk, sk) if url and pk and sk else None

    def _post(self, path: str, body: dict) -> dict:
        from urllib.request import Request

        req = Request(f"{self.url}{path}", data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": self.auth, "Content-Type": "application/json"})
        with self.opener.open(req, timeout=20) as resp:
            raw = resp.read().decode() or "{}"
        return json.loads(raw)

    def sync(self, cases: list[EvalCase], run_name: str, results: list[dict], *, content_classes: tuple[str, ...] = ("general",)) -> int:
        """One dataset per phase; each case an item (its documents only for `content_classes`); this
        run's traces linked to the items, with the run's scores on them. Returns items written."""
        by_id, written = {c.id: c for c in cases}, 0
        for phase in sorted({c.phase for c in cases}):
            try:
                self._post("/api/public/datasets", {"name": f"valxel/{phase}",
                                                    "description": f"The SDLC tool's {phase} documents and what people decided"})
            except Exception:
                pass  # it exists
        for r in results:
            c = by_id.get(r["case_id"])
            if c is None:
                continue
            content = c.data_class in content_classes
            try:
                self._post("/api/public/dataset-items", {
                    "id": c.id.replace(":", "-"), "datasetName": f"valxel/{c.phase}",
                    "input": ({"request": c.ctx.get("description", ""), "parents": c.ctx.get("parent_texts", {})}
                              if content else {"case": c.id}),
                    "expectedOutput": c.reference if content and c.reference else None,
                    "metadata": {"product": c.product_id, "feature": c.feature_id, "data_class": c.data_class,
                                 "label": c.label}})
                written += 1
                for trace_id in r.get("trace_ids") or []:
                    self._post("/api/public/dataset-run-items", {"runName": run_name, "datasetItemId": c.id.replace(":", "-"),
                                                                 "traceId": trace_id})
                    for name in ("passed", "must_met", "critic_agrees"):
                        v = r.get(name)
                        if v is not None:
                            self._post("/api/public/scores", {"traceId": trace_id, "name": f"valxel.{name}",
                                                              "value": float(v), "comment": run_name})
            except Exception as exc:
                log.warning("Langfuse: %s not synced: %s", c.id, exc)
        return written


# ---------------------------------------------------------------------------------------- the config gate

class ConfigGate:
    """Evals for a pull request to the config repo: the cases replayed under the config at the PR's
    head and at its base, compared, and the result posted on the head commit as `valxel / evals`.
    Runs where the cases and the model keys are (the tool), never in the config repo's CI."""

    CONTEXT = "valxel / evals"

    def __init__(self, services, github, *, config_repo: str, public_url: str = "", limit_per_phase: int = 5,
                 budget_usd: float = 20.0, max_drop: float = 0.05, data_classes: tuple[str, ...] = ("general",),
                 pairwise: bool = False, background: bool = True) -> None:
        self.services, self.github, self.config_repo = services, github, config_repo
        self.public_url = public_url.rstrip("/")
        self.limit_per_phase, self.budget_usd, self.max_drop = limit_per_phase, budget_usd, max_drop
        self.data_classes, self.pairwise, self.background = data_classes, pairwise, background
        self.runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def from_env(services, github, env=None) -> "ConfigGate | None":
        import os

        env = env if env is not None else os.environ
        repo = env.get("VALXEL_CONFIG_REPO", "")
        if not repo:
            return None
        return ConfigGate(services, github, config_repo=repo, public_url=env.get("VALXEL_PUBLIC_URL", ""),
                          limit_per_phase=int(env.get("VALXEL_EVAL_CASES_PER_PHASE", "5")),
                          budget_usd=float(env.get("VALXEL_EVAL_BUDGET_USD", "20")),
                          max_drop=float(env.get("VALXEL_EVAL_MAX_DROP", "0.05")),
                          data_classes=tuple(c for c in env.get("VALXEL_EVAL_DATA_CLASSES", "general").split(",") if c),
                          pairwise=env.get("VALXEL_EVAL_PAIRWISE", "") in ("1", "true"))

    def _status(self, sha: str, state: str, description: str, run_id: str) -> None:
        try:
            self.github.set_status(repo=self.config_repo, sha=sha, context=self.CONTEXT, state=state,
                                   description=description[:140],
                                   url=f"{self.public_url}/api/evals/runs/{run_id}" if self.public_url else "")
        except Exception:
            log.warning("evals: couldn't post the status on %s@%s", self.config_repo, sha[:7], exc_info=True)

    def start(self, ref: str, base_ref: str = "", requested_by: str = "") -> dict:
        run_id = f"eval-{ref[:12]}-{int(time.time())}"
        run = {"id": run_id, "repo": self.config_repo, "ref": ref, "base_ref": base_ref, "requested_by": requested_by,
               "status": "running", "started_at": now(), "finished_at": None, "comparison": None, "markdown": ""}
        with self._lock:
            self.runs[run_id] = run
        self._status(ref, "pending", "Replaying past documents under this config", run_id)
        if self.background:
            threading.Thread(target=self._run, args=(run_id,), name=f"valxel-{run_id}", daemon=True).start()
        else:
            self._run(run_id)
        return dict(run)

    def _run(self, run_id: str) -> None:
        run = self.runs[run_id]
        settings = self.services.settings
        try:
            cases = self.services.evals.cases(data_classes=self.data_classes, limit_per_phase=self.limit_per_phase)
            if not cases:
                run |= {"status": "passed", "comparison": {"ok": True, "regressions": [], "metrics": {}, "cases": 0,
                                                            "max_drop": self.max_drop}}
                self._status(run["ref"], "success", "No labelled cases yet: nothing to compare", run_id)
                return
            cand_dir = config_dir_at(self.github, self.config_repo, run["ref"], settings.config_dir)
            base_dir = (config_dir_at(self.github, self.config_repo, run["base_ref"], settings.config_dir)
                        if run["base_ref"] else settings.config_dir)
            base = run_cases(services_for(settings, base_dir), cases, pairwise=self.pairwise, budget_usd=self.budget_usd)
            cand = run_cases(services_for(settings, cand_dir), cases, pairwise=self.pairwise, budget_usd=self.budget_usd)
            cmp = compare(base, cand, max_drop=self.max_drop)
            run |= {"status": "passed" if cmp["ok"] else "failed", "comparison": cmp,
                    "markdown": markdown(cmp, base, cand), "summary": {"baseline": base["summary"], "candidate": cand["summary"]}}
            self._status(run["ref"], "success" if cmp["ok"] else "failure", headline(cmp), run_id)
            lf = Langfuse.from_env()
            if lf is not None:
                lf.sync(cases, run_id, cand["results"], content_classes=("general",))
        except Exception as exc:
            log.warning("evals: run %s failed", run_id, exc_info=True)
            run |= {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            self._status(run["ref"], "error", f"The eval run failed: {type(exc).__name__}: {exc}", run_id)
        finally:
            run["finished_at"] = now()


run_cases = run  # the gate's name for `run`, which its own method shadows
