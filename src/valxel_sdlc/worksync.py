"""Keep the product's work tracker in step with a feature, outside the agent graph.

After every run of a feature thread the engine calls `WorkSync.sync`. It works out the items
the feature should have from its state (desired state), compares them with what it has already
created (the link store), and creates, moves or comments only on the difference. A tracker
outage therefore never stops agents or gates: the feature records the error and the next run
catches up.

  G1 approved   -> epic                                  (in progress once G2 is approved)
  G2 approved   -> one story per acceptance criterion and NFR (in progress at build; done when its
                   test cases pass, or at merge for older plans; cancelled when deferred)
  plan passed   -> one task per planned repo change      (in progress with its PR, done at merge)
                -> one sub-task per test case, under its requirement's story
                   (in progress once its test is written, done when it passes)
  any decision  -> a comment on the epic (who, what, feedback); diagrams likewise
  released      -> epic done;  stopped -> open items cancelled

`reconcile` reads the items back and reports drift: an item closed, reopened, renamed or deleted in
the tracker that the tool's matrix disagrees with, or one missing. G6 shows it as a warning, or
blocks on it where the product sets `work_tracking.block_on_drift: true`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from .agents.parse import parse_acs, parse_nfrs
from .integrations.worktracking import STATUS_ORDER, WorkTracker

GATE_NAMES = {"g1": "G1 Intent", "g2": "G2 Scope", "g3": "G3 Experience", "g4": "G4 Architecture",
              "g5": "G5 Code", "g6": "G6 Release"}
VERB = {"approve": "approved", "revise": "sent back", "takeover": "taken over", "handback": "handed back",
        "abort": "stopped", "resolved": "resolved", "retry": "sent back to agents",
        "accept_risk": "accepted as a risk"}


@dataclass
class Desired:
    key: str  # "epic", "AC-1", "NFR-1", "TASK-1", "TC-1"
    kind: str  # epic | story | task | subtask
    summary: str
    description: str
    status: str  # todo | in_progress | done | cancelled
    parent: str | None = None


class LinkStore(Protocol):
    def get(self, feature_id: str) -> dict[str, dict]: ...
    def put(self, feature_id: str, key: str, row: dict) -> None: ...


class InMemoryLinkStore:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    def get(self, feature_id):
        with self._lock:
            return {k: dict(v) for k, v in self._rows.get(feature_id, {}).items()}

    def put(self, feature_id, key, row):
        with self._lock:
            self._rows.setdefault(feature_id, {})[key] = dict(row)


class PostgresLinkStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS valxel_work_items (
      feature_id TEXT NOT NULL, key TEXT NOT NULL, row JSONB NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (feature_id, key)
    );
    """

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            conn.execute(self.DDL)

    def get(self, feature_id):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT key, row FROM valxel_work_items WHERE feature_id = %s", (feature_id,)).fetchall()
        return {r["key"]: dict(r["row"]) for r in rows}

    def put(self, feature_id, key, row):
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO valxel_work_items (feature_id, key, row) VALUES (%s, %s, %s) "
                "ON CONFLICT (feature_id, key) DO UPDATE SET row = EXCLUDED.row, updated_at = now()",
                (feature_id, key, Jsonb(row)))


def _ended(s: dict) -> str | None:
    return {"done": "done", "aborted": "cancelled"}.get(s.get("status") or "")


CASE_STATUS = {"passed": "done", "written": "in_progress", "failed": "in_progress"}


def desired_items(s: dict, *, prd_text: str, feature_url: str, restricted_repos: dict[str, str],
                  trace: dict | None = None) -> list[Desired]:
    """What the tracker should hold for this feature state. `restricted_repos` maps a repo to the
    lane whose data class may not be sent to this tracker; such tasks carry no detail."""
    fid, title = s["feature_id"], s["title"]
    approvals = s.get("approvals") or {}
    ended = _ended(s)
    merged = bool((s.get("change_set") or {}).get("merged"))
    building = bool(s.get("pull_requests")) or (s.get("phase_status") or {}).get("build") is not None
    out: list[Desired] = []
    if not approvals.get("g1"):
        return out

    epic_status = ended or ("in_progress" if approvals.get("g2") else "todo")
    out.append(Desired("epic", "epic", f"[{fid}] {title}",
                       f"{s.get('description') or title}\n\nTracked by the Valxel SDLC: {feature_url}",
                       epic_status))
    reqs = {r["id"]: r for r in (trace or {}).get("criteria", []) + (trace or {}).get("nfrs", [])}
    if approvals.get("g2"):
        nfrs = parse_nfrs(prd_text)
        stories = [(ac_id, f"[{fid}/{ac_id}] {text}", f"Acceptance criterion {ac_id} of {fid}.\n\n{text}")
                   for ac_id, text in parse_acs(prd_text).items()]
        stories += [(n, f"[{fid}/{n}] ({spec['kind']}) {spec['text']}",
                     f"Non-functional requirement {n} ({spec['kind']}) of {fid}.\n\n{spec['text']}")
                    for n, spec in nfrs.items()]
        for rid, summary, desc in stories:
            req = reqs.get(rid) or {}
            cases = req.get("cases") or []
            if req.get("deferred") is not None:
                status, desc = "cancelled", f"{desc}\n\nDeferred: {req['deferred']}"
            elif cases:  # done when its test cases pass, not merely when the code merges
                status = ("done" if all(c["status"] == "passed" for c in cases)
                          else "in_progress" if building else "todo")
            else:
                status = "done" if merged else ("in_progress" if building else "todo")
            if ended == "cancelled" and status != "done":
                status = "cancelled"
            out.append(Desired(rid, "story", summary, desc, status, parent="epic"))
    prs = s.get("pull_requests") or {}
    for t in s.get("tasks") or []:
        mine = [pr for pr in prs.values() if pr.get("task_id") == t["id"]]
        status = ("done" if mine and all(p.get("state") == "merged" for p in mine)
                  else "in_progress" if mine else "todo")
        if ended == "cancelled" and status != "done":
            status = "cancelled"
        lane = restricted_repos.get(t["repo"])
        if lane:
            summary = f"[{fid}/{t['id']}] Change in {lane}"
            desc = f"Details stay on the feature page: {lane}'s data class does not allow this tracker. {feature_url}"
        else:
            summary = f"[{fid}/{t['id']}] {t['repo']}: {', '.join(t.get('acs') or []) or 'planned change'}"
            desc = "\n".join([f"Repo: {t['repo']} ({t.get('role', '')})", "Files:",
                              *[f"- {f}" for f in t.get("files", [])],
                              *[f"Pull request: {p['url']}" for p in mine]])
        out.append(Desired(t["id"], "task", summary, desc, status, parent="epic"))
    if approvals.get("g2"):  # the test cases, as sub-tasks of their requirement's story
        repo_of = {t["id"]: t["repo"] for t in s.get("tasks") or []}
        for rid, req in reqs.items():
            for c in req.get("cases") or []:
                status = CASE_STATUS.get(c["status"], "todo")
                if ended == "cancelled" and status != "done":
                    status = "cancelled"
                lane = restricted_repos.get(repo_of.get(c.get("task", ""), ""))
                summary = (f"[{fid}/{c['id']}] ({rid}, {c['kind']}) test in {lane}" if lane
                           else f"[{fid}/{c['id']}] ({rid}, {c['kind']}) {c['what']}")
                desc = (f"Details stay on the feature page: {lane}'s data class does not allow this tracker. "
                        f"{feature_url}" if lane else
                        f"Test case {c['id']} of {rid}: {c['what']}\nTest: {c['ref']}\nStatus: {c['status']}")
                out.append(Desired(c["id"], "subtask", summary[:250], desc, status, parent=rid))
    return out


def decision_comments(s: dict) -> list[str]:
    out = []
    for g in s.get("gate_decisions") or []:
        name = GATE_NAMES.get(g["gate"]) or f"{g.get('phase', '')} escalation"
        lane = f" for {g['lane']}" if g.get("lane") and g.get("lane") != s.get("product_id") else ""
        text = f"{name}{lane} {VERB.get(g['decision'], g['decision'])} by {g['approver']}."
        if g.get("feedback"):
            text += f" Feedback: {g['feedback']}"
        out.append(text)
    return out


def diagram_comments(s: dict) -> list[str]:
    return [f"Architecture diagram v{d['version']} ({d['provider']}): {d['url']}"
            for d in s.get("diagrams") or [] if d.get("url")]


class WorkSync:
    def __init__(self, services, links: LinkStore, public_url: str = "http://localhost:8080") -> None:
        self.services = services
        self.links = links
        self.public_url = public_url.rstrip("/")
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, fid: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(fid, threading.Lock())

    def config(self, s: dict) -> tuple[dict | None, WorkTracker | None]:
        snap = self.services.snapshot(s["product_id"], s.get("governance"))
        cfg = snap.product.tools.get("work_tracking")
        if not cfg:
            return None, None
        return cfg, self.services.trackers.get(cfg["provider"])

    def _load(self, s: dict):
        arts = s.get("artifacts") or {}

        def load(kind: str) -> str:
            try:
                return self.services.artifacts.load(arts[kind]) if kind in arts else ""
            except Exception:
                return ""
        return load

    def desired(self, s: dict, cfg: dict) -> list[Desired]:
        from . import traceability

        snap = self.services.snapshot(s["product_id"], s.get("governance"))
        tools = self.services.settings.tools
        restricted = {repo: lane.product_id for repo, lane in snap.lane_of_repo().items()
                      if not tools.allowed(lane.data_class, cfg["provider"])}
        load = self._load(s)
        return desired_items(s, prd_text=load("PRD"), feature_url=f"{self.public_url}/features/{s['feature_id']}",
                             restricted_repos=restricted, trace=traceability.build(s, load))

    def reconcile(self, s: dict) -> dict:
        """The tracker against the tool's matrix: {"drift": [...], "block": bool}. Read only."""
        cfg, tracker = self.config(s)
        if not cfg or tracker is None:
            return {"drift": [], "block": False}
        fid, have, drift = s["feature_id"], self.links.get(s["feature_id"]), []
        names = {"todo": "open", "in_progress": "in progress", "done": "done", "cancelled": "cancelled"}
        for d in self.desired(s, cfg):
            row = have.get(d.key)
            label = f"{d.key} ({d.kind})" if d.key != "epic" else "the epic"
            if row is None:
                drift.append(f"{label} is missing from the tracker")
                continue
            got = tracker.get(cfg, row)
            if got is None:
                drift.append(f"{label} ({row.get('key', '')}) was deleted in the tracker")
                continue
            ref = row.get("key", "")
            closed = {"done", "cancelled"}
            if got["status"] in closed and d.status not in closed:
                drift.append(f"{label} ({ref}) is {names[got['status']]} in the tracker, but {names[d.status]} here")
            elif d.status in closed and got["status"] not in closed:
                drift.append(f"{label} ({ref}) is {names[d.status]} here, but {names[got['status']]} in the tracker")
            if got.get("summary") and got["summary"].strip() != str(row.get("summary", "")).strip():
                drift.append(f"{label} ({ref}) was renamed in the tracker: {got['summary'][:120]!r}")
        return {"drift": drift, "block": bool(cfg.get("block_on_drift"))}

    def sync(self, s: dict) -> dict:
        """Reconcile one feature. Returns {"provider", "created", "moved", "commented"}."""
        cfg, tracker = self.config(s)
        if not cfg or tracker is None:
            return {}
        fid = s["feature_id"]
        with self._lock(fid):
            want = self.desired(s, cfg)
            have = self.links.get(fid)
            done = {"provider": cfg["provider"], "created": 0, "moved": 0, "commented": 0}

            for d in want:  # epic first, so children can point at it
                row = have.get(d.key)
                if row is None:
                    marker = f"{fid}/{d.key}" if d.key != "epic" else fid  # the summary's [prefix]
                    parent = have.get(d.parent) if d.parent else None
                    if d.key == "epic" and s.get("initiative_id"):  # the feature sits under its initiative
                        parent = self.links.get(s["initiative_id"]).get("initiative")
                    ext = tracker.find(cfg, marker) or tracker.create(
                        cfg, kind=d.kind, summary=d.summary, description=d.description, marker=marker,
                        parent=parent)
                    row = {**ext, "kind": d.kind, "summary": d.summary, "status": "todo"}
                    self.links.put(fid, d.key, row)
                    have[d.key] = row
                    done["created"] += 1
                if STATUS_ORDER[d.status] > STATUS_ORDER.get(row["status"], 0):  # never move backwards
                    tracker.set_status(cfg, row, d.status)
                    row = {**row, "status": d.status}
                    self.links.put(fid, d.key, row)
                    have[d.key] = row
                    done["moved"] += 1

            epic = have.get("epic")
            if epic:
                for stream, texts in (("_decisions", decision_comments(s)), ("_diagrams", diagram_comments(s))):
                    posted = int(have.get(stream, {}).get("count", 0))
                    for text in texts[posted:]:
                        tracker.comment(cfg, epic, text)
                        posted += 1
                        self.links.put(fid, stream, {"count": posted})  # after each, so a retry resumes
                        done["commented"] += 1
            return done

    def sync_initiative(self, s: dict) -> dict:
        """An approved initiative gets one tracker item (its features' epics sit under it); a dropped
        one that already has an item is cancelled. Drafts stay in the tool."""
        cfg, tracker = self.config(s)
        if not cfg or tracker is None or s.get("status") not in ("approved", "dropped"):
            return {}
        iid = s["initiative_id"]
        with self._lock(iid):
            have = self.links.get(iid)
            row = have.get("initiative")
            if row is None and s["status"] == "approved":
                d = s.get("draft") or {}
                desc = (f"{d.get('outcome', '')}\n\nSuccess measure: {d.get('measure', '')}\nHorizon: {d.get('horizon', '')}\n"
                        f"Budget envelope: {d.get('budget_usd', 0):.0f} USD\n\nTracked by the Valxel SDLC: "
                        f"{self.public_url}/initiatives/{iid}")
                ext = tracker.find(cfg, iid) or tracker.create(cfg, kind="initiative", summary=f"[{iid}] {s['title']}",
                                                                description=desc, marker=iid, parent=None)
                row = {**ext, "kind": "initiative", "summary": s["title"], "status": "todo"}
                self.links.put(iid, "initiative", row)
            if row is None:
                return {}
            want = "in_progress" if s["status"] == "approved" else "cancelled"
            if STATUS_ORDER[want] > STATUS_ORDER.get(row["status"], 0):
                tracker.set_status(cfg, row, want)
                self.links.put(iid, "initiative", {**row, "status": want})
            return {"provider": cfg["provider"], "initiative": row.get("key") or row.get("id")}

    def sync_incident(self, s: dict) -> dict:
        """One tracker item per incident (a Bug by default), in progress until closed, with a comment
        for each action taken. Details stay out: titles of alerts, never patient data."""
        cfg, tracker = self.config(s)
        if not cfg or tracker is None:
            return {}
        iid = s["incident_id"]
        with self._lock(iid):
            have = self.links.get(iid)
            row = have.get("incident")
            if row is None:
                desc = (f"Severity {s.get('severity')}. {s.get('title')}\n\nTracked by the Valxel SDLC: "
                        f"{self.public_url}/incidents/{iid}")
                ext = tracker.find(cfg, iid) or tracker.create(cfg, kind="incident", summary=f"[{iid}] {s['title']}",
                                                                description=desc, marker=iid, parent=None)
                row = {**ext, "kind": "incident", "summary": s["title"], "status": "todo"}
                self.links.put(iid, "incident", row)
            want = "done" if s.get("status") == "closed" else "in_progress"
            if STATUS_ORDER[want] > STATUS_ORDER.get(row["status"], 0):
                tracker.set_status(cfg, row, want)
                row = {**row, "status": want}
                self.links.put(iid, "incident", row)
            notes = [f"{t['type']} by {t.get('by', '')}: {t.get('result', '')}" for t in s.get("taken") or []]
            posted = int(have.get("_actions", {}).get("count", 0))
            for text in notes[posted:]:
                tracker.comment(cfg, row, text)
                posted += 1
                self.links.put(iid, "_actions", {"count": posted})
            return {"provider": cfg["provider"], "incident": row.get("key") or row.get("id")}

    def sync_release(self, s: dict) -> dict:
        """Releases are noted on each member feature's epic: when planned, and when approved or cancelled."""
        cfg, tracker = self.config(s)
        if not cfg or tracker is None:
            return {}
        rid = s["release_id"]
        kind = "launch group" if s.get("model") == "launch" else "release train"
        notes = [f"Part of {rid} ({kind}){' targeting ' + s['target_date'] if s.get('target_date') else ''}."]
        if s.get("status") in ("approved", "shipped"):
            notes.append(f"{rid} approved by {s.get('approved_by', '')}"
                         + ("; flags are on, the feature is live." if s.get("status") == "shipped" else "."))
        elif s.get("status") == "cancelled":
            notes.append(f"{rid} was cancelled.")
        posted = 0
        for fid in s.get("features") or []:
            with self._lock(fid):
                have = self.links.get(fid)
                epic = have.get("epic")
                if not epic:
                    continue
                key = f"_release:{rid}"
                done = int(have.get(key, {}).get("count", 0))
                for text in notes[done:]:
                    tracker.comment(cfg, epic, text)
                    done += 1
                    posted += 1
                    self.links.put(fid, key, {"count": done})
        return {"provider": cfg["provider"], "commented": posted}

    def items(self, feature_id: str) -> list[dict]:
        rows = self.links.get(feature_id)
        order = {"epic": 0, "story": 1, "task": 2}
        # "item" is our key (epic, AC-1, TASK-1); "key" is the tracker's (FARM-12, #431).
        return sorted(({**v, "item": k} for k, v in rows.items() if not k.startswith("_")),
                      key=lambda r: (order.get(r.get("kind"), 9), int(r["item"].rsplit("-", 1)[-1])
                                     if r["item"][-1:].isdigit() else 0))
