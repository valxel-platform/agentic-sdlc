"""Scan upkeep, run by the engine: intake, triage, fix features, deadlines, Dependabot.

    receive(event, payload)   a GitHub alert webhook: record it, triage the product
    sweep()                   nightly: every managed repo's open alerts on its default branch, so a
                              missed webhook (or one GitHub never sent) is caught within a day
    tick()                    every 15 minutes on the elected watcher: expired risk acceptances,
                              triage, fix features started (up to max_open_fixes) and followed,
                              deadlines, and Dependabot's version updates
    fix_window(packet)        a held fix's wait before G6: joins the next release inside its
                              deadline, or goes to its own G6 on its release-by date
    accept(...)               a security approver accepts the risk until a date (moves the deadline)

Fix features are the hotfix track (graph/briefs.py make_hotfix_brief): the policy, which governance
owns, decided the fix is needed, so they start at planning. Tests, scans, G5 for risky changes and
G6 stay. G5 goes to the lane's security approvers; G6 to its release approvers as usual.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta

from . import alerts as alert_mod
from . import deadline as deadline_for
from . import decide, effective, update_type
from . import release_by as release_by_for
from .register import CLOSED, InMemoryFindingRegister

log = logging.getLogger(__name__)
AGENT = "valxel-upkeep"  # who the tool acts as when the policy decides (gates, merges, history)
MAX_ATTEMPTS = 2  # fix features per finding before a person is asked
_BUMP = re.compile(r"\bfrom\s+v?([0-9][\w.+-]*)\s+to\s+v?([0-9][\w.+-]*)", re.IGNORECASE)
_BUMP_PKG = re.compile(r"[Bb]umps?\s+(?:\[)?`?([@\w./:-]+?)`?(?:\])?(?:\([^)]*\))?\s+from\s+v?[0-9]")
_UPDATES_PKG = re.compile(r"Updates?\s+`?([@\w./:-]+?)`?\s+from\s+v?[0-9]")


def _today() -> date:
    return datetime.now(UTC).date()


def short(repo: str) -> str:
    return repo.split("/", 1)[-1]


class Upkeep:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.services = engine.services
        if getattr(self.services, "findings", None) is None:
            self.services.findings = InMemoryFindingRegister()
        self._repos: tuple[float, dict[str, str]] | None = None
        self._lock = threading.Lock()

    @property
    def register(self):
        return self.services.findings

    # ------------------------------------------------------------------ policy and repos

    def policy(self, product_id: str) -> dict:
        try:
            pol = getattr(self.services.snapshot(product_id).product, "upkeep", None)
            if pol:
                return pol
        except Exception:
            pass
        entry = self.services.settings.registry.get(product_id)
        q = self.services.settings.quality
        return effective(getattr(q, "upkeep", None), entry.data_class if entry else "general", None)

    def repos(self, refresh: bool = False) -> dict[str, str]:
        """Managed repo -> product: each lane's repos and its governance repo (repo_standards)."""
        with self._lock:
            if self._repos and not refresh and time.monotonic() - self._repos[0] < 3600:
                return self._repos[1]
        from ..repo_standards import targets

        found = {t.repo: t.product for t in targets(self.services) if t.kind in ("lane", "governance")
                 and t.product in self.services.settings.registry}
        with self._lock:
            self._repos = (time.monotonic(), found)
        return found

    # ------------------------------------------------------------------ intake

    def receive(self, event_name: str, payload: dict) -> dict | None:
        f = alert_mod.from_event(event_name, payload)
        if f is None:
            return None
        pid = self.repos().get(f["repo"]) or self.repos(refresh=True).get(f["repo"])
        if not pid:
            log.warning("alert %s is on %s, which no product manages", f["id"], f["repo"])
            return None
        rec = self.take(pid, f)
        self.triage(pid)
        return self.register.get(rec["id"])

    def take(self, product_id: str, f: dict, waivers: dict | None = None) -> dict:
        rec, change = self.register.upsert(product_id, f)
        if change in ("new", "reopened") and rec["state"] == "open":
            w = (waivers if waivers is not None else self.feature_waivers(product_id)).get(rec["id"])
            if w:
                rec = self.register.update(rec["id"], f"risk accepted at {w.get('feature_id', 'a feature')}'s "
                                           f"security review by {w.get('approver')}", status="accepted",
                                           accepted={k: w.get(k) for k in ("approver", "reason", "expires")})
            elif rec["kind"] == "secret":
                rec = self._secret_opened(rec)
        elif change == "closed":
            rec = self._closed(rec)
        return rec

    def sweep(self, product_id: str | None = None) -> dict:
        """Every managed repo's open alerts on its default branch. An alert the register has open
        that GitHub no longer lists was closed while no webhook reached us."""
        gh = self.services.github
        seen = {"repos": 0, "alerts": 0, "closed": 0}
        waivers: dict[str, dict] = {}
        for repo, pid in sorted(self.repos(refresh=True).items()):
            if product_id and pid != product_id:
                continue
            if pid not in waivers:
                waivers[pid] = self.feature_waivers(pid)
            seen["repos"] += 1
            lists = {"code": lambda r=repo: gh.code_scanning_alerts(repo=r),
                     "dependency": lambda r=repo: gh.dependabot_alerts(repo=r),
                     "secret": lambda r=repo: gh.secret_alerts(repo=r)}
            for kind, fetch in lists.items():
                try:
                    listed = fetch()
                except Exception as exc:  # one repo's API trouble doesn't stop the sweep
                    log.warning("sweep: %s %s alerts: %s", repo, kind, exc)
                    continue
                if listed is None:  # the feature is off for the repo: the repo check reports that
                    continue
                ids = set()
                for a in listed:
                    f = alert_mod.NORMALIZE[kind](repo, a)
                    if f["state"] != "open":
                        continue
                    ids.add(f["id"])
                    self.take(pid, f, waivers[pid])
                    seen["alerts"] += 1
                for rec in self.register.list(pid):
                    if rec["repo"] == repo and rec["kind"] == kind and rec["state"] == "open" and rec["id"] not in ids:
                        self.take(pid, {**rec, "state": "fixed"})
                        seen["closed"] += 1
        return seen

    def feature_waivers(self, product_id: str) -> dict[str, dict]:
        """Risks accepted at features' security reviews that haven't expired, by fingerprint."""
        from ..quality import active_waivers

        out: dict[str, dict] = {}
        for row in self.engine._rows(product_id, "feature"):
            try:
                ws = self.engine._state(row["feature_id"]).get("waivers") or []
            except Exception:
                continue
            live = active_waivers(ws)
            for w in ws:
                if w.get("fingerprint") in live:
                    out[w["fingerprint"]] = {**w, "feature_id": row["feature_id"]}
        return out

    # ------------------------------------------------------------------ triage and fixes

    def tick(self, today: date | None = None) -> dict:
        today = today or _today()
        done: dict = {"merged": [], "started": [], "expired": [], "overdue": []}
        products = sorted({r["product_id"] for r in self.register.list()} | set(self.repos().values()))
        for pid in products:
            try:
                done["expired"] += self.expire(pid, today)
                self.follow(pid, today)
                self.triage(pid, today)
                done["started"] += self.start_fixes(pid, today)
                done["overdue"] += self.deadlines(pid, today)
            except Exception as exc:
                log.warning("upkeep tick for %s: %s", pid, exc)
            try:
                done["merged"] += self.dependabot(pid)
            except Exception as exc:
                log.warning("dependabot for %s: %s", pid, exc)
        return done

    def expire(self, product_id: str, today: date) -> list[str]:
        out = []
        for rec in self.register.list(product_id, ("accepted",)):
            exp = (rec.get("accepted") or {}).get("expires") or ""
            if exp and exp[:10] <= today.isoformat():
                # The clock starts again from the expiry: the deadline is the policy's from then.
                self.register.update(rec["id"], f"accepted risk expired on {exp[:10]}", status="open", outcome="",
                                     first_seen=today.isoformat(), accepted=None)
                out.append(rec["id"])
        return out

    def triage(self, product_id: str, today: date | None = None) -> list[dict]:
        today = today or _today()
        pol = self.policy(product_id)
        out = []
        for rec in self.register.list(product_id, ("open",)):
            if rec["kind"] == "secret":
                out.append(self._secret_opened(rec))
                continue
            outcome, why = decide(pol, rec)
            due = deadline_for(pol, rec["severity"], rec["first_seen"])
            status = "backlog" if outcome == "backlog" else "planned"
            if rec.get("attempts", 0) >= MAX_ATTEMPTS:
                status, why = "escalated", f"{rec['attempts']} fixes didn't close it; {why}"
            out.append(self.register.update(rec["id"], f"triaged: {outcome} ({why})", status=status, outcome=outcome,
                                            why=why, deadline=due, release_by=release_by_for(pol, due)))
        # A scheduled finding can become urgent (EPSS rose, the policy tightened): re-decide before a fix starts.
        for rec in self.register.list(product_id, ("planned",)):
            outcome, why = decide(pol, rec)
            if outcome != rec["outcome"]:
                self.register.update(rec["id"], f"re-triaged: {outcome} ({why})", outcome=outcome, why=why)
        return out

    def start_fixes(self, product_id: str, today: date | None = None) -> list[str]:
        """One fix feature per repo and outcome, up to the product's max_open_fixes in flight. A
        dependency finding first waits dependabot_grace_hours for Dependabot's own pull request."""
        pol = self.policy(product_id)
        recs = self.register.list(product_id)
        busy = {r["fix_feature"] for r in recs if r["status"] == "fixing" and r.get("fix_feature")}
        room = max(0, int(pol.get("max_open_fixes", 5)) - len(busy))
        grace = timedelta(hours=int(pol.get("dependabot_grace_hours") or 0))
        now = datetime.now(UTC)
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in recs:
            if r["status"] != "planned":
                continue
            if r["kind"] == "dependency" and r["outcome"] != "hotfix" and grace:
                seen = datetime.fromisoformat(str(r["first_seen"]).replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=UTC)
                if now - seen < grace:
                    continue
            groups.setdefault((r["repo"], r["outcome"]), []).append(r)
        # Hotfixes first, then the nearest deadline.
        order = sorted(groups.items(), key=lambda kv: (kv[0][1] != "hotfix", min(r["deadline"] or "9999" for r in kv[1])))
        started = []
        for (repo, outcome), group in order:
            if room <= 0:
                break
            try:
                fid = self.start_fix(product_id, repo, group, outcome)
            except Exception as exc:
                log.warning("couldn't start a fix for %s: %s", repo, exc)
                continue
            for r in group:
                self.register.update(r["id"], f"fix started: {fid}", status="fixing", fix_feature=fid,
                                     attempts=int(r.get("attempts") or 0) + 1)
            started.append(fid)
            room -= 1
        return started

    def start_fix(self, product_id: str, repo: str, group: list[dict], outcome: str) -> str:
        from ..quality import sev_rank

        worst = max(group, key=lambda r: sev_rank(r["severity"]))
        hold = outcome != "hotfix"
        by = min((r["release_by"] for r in group if r.get("release_by")), default="")
        lines = []
        for r in group:
            if r["kind"] == "dependency":
                what = f"upgrade {r['package']} to {r['patched'] or 'a fixed version'} or later" + (
                    f" in {r['path']}" if r.get("path") else "")
            else:
                what = f"fix {r['tool']} {r['rule']}" + (f" at {r['path']}" if r.get("path") else "")
            lines.append(f"- {r['severity'].upper()} {r['title']}: {what} ({r['url']})")
        change = (f"Close {len(group)} security finding{'s' if len(group) > 1 else ''} on {short(repo)}'s default "
                  f"branch:\n" + "\n".join(lines))
        title = f"Security fix: {short(repo)}: {worst['title']}"[:150] if len(group) == 1 else \
            f"Security fix: {short(repo)}: {len(group)} findings"
        return self.engine.create(
            product_id=product_id, title=title, created_by=AGENT, background=True, track="hotfix",
            description=change + ("\n\nHeld for the next release inside its deadline; released on its own on "
                                  f"{by} if there isn't one." if hold and by else ""),
            not_duplicate_reason=f"Fix for {', '.join(r['id'] for r in group)}"[:500],
            hotfix={"source": "finding", "findings": [r["id"] for r in group], "repo": repo, "change": change,
                    "severity": worst["severity"], "approved_by": AGENT, "outcome": outcome, "hold": hold,
                    "release_by": by, "why": worst.get("why", "")})

    def follow(self, product_id: str, today: date) -> None:
        """Fix features that ended: stopped by a person, or released while the alert is still open."""
        for rec in self.register.list(product_id, ("fixing",)):
            fid = rec.get("fix_feature")
            if not fid:
                continue
            try:
                s = self.engine._state(fid)
            except Exception:
                continue
            st = s.get("status")
            if st == "aborted":
                self.register.update(rec["id"], f"{fid} was stopped", status="open", fix_feature="")
            elif st == "done":
                self.register.update(rec["id"], f"{fid} was released but GitHub still has the alert open",
                                     status="open", fix_feature="")

    def deadlines(self, product_id: str, today: date) -> list[str]:
        """A finding past its deadline, or one the fixes didn't close, needs a person: an incident
        (no component, so nothing is rolled back) that resolves when the finding closes."""
        out = []
        for rec in self.register.list(product_id, ("open", "planned", "fixing", "escalated")):
            late = bool(rec.get("deadline")) and rec["deadline"] < today.isoformat()
            if not (late or rec["status"] == "escalated") or rec.get("raised"):
                continue
            why = (f"its fix deadline, {rec['deadline']}, passed" if late else rec.get("why") or "the fixes didn't close it")
            sig = self._signal(rec, "firing", f"Security finding needs a person: {rec['title']}"[:200],
                               f"{rec['severity'].upper()} {rec['title']} in {rec['repo']}: {why}. "
                               + (f"A fix is in flight: {rec['fix_feature']}. " if rec.get("fix_feature") else "")
                               + "Fix it, or accept the risk until a date (security approvers): "
                               + f"POST /api/findings/{rec['id']}/accept. {rec['url']}", "sev3")
            touched = self.engine.ingest([sig], background=True)
            self.register.update(rec["id"], f"raised: {why}", raised=True,
                                 incident_id=touched[0] if touched else rec.get("incident_id", ""))
            out.append(rec["id"])
        return out

    # ------------------------------------------------------------------ secrets

    def _secret_opened(self, rec: dict) -> dict:
        """An exposed secret is an incident: on-call is paged for an active or public one, the
        incident says to rotate first, then remove it from the code (a hotfix on-call approves)."""
        if rec.get("incident_id") and rec["status"] == "incident":
            return rec
        live = rec.get("validity") == "active" or rec.get("publicly_leaked")
        detail = (f"{rec['title']} in {rec['repo']} ({rec['url']}). The repo is public, so treat it as known to "
                  "others. 1) Rotate it now: revoke it at its provider, or rotate it in the secret store "
                  "(valxel_sdlc.secret_ring keeps the previous value working while services move over). "
                  f"2) Remove it from the code with a hotfix on {rec['repo']}, reading it from the secret store "
                  "instead. 3) Close the alert in GitHub as revoked. The incident resolves when it closes.")
        sig = self._signal(rec, "firing", f"Exposed secret: {rec['title']} in {short(rec['repo'])}"[:200], detail,
                           "sev1" if live else "sev2")
        touched = self.engine.ingest([sig], background=True)
        return self.register.update(rec["id"], "incident opened" + (f": {touched[0]}" if touched else ""),
                                    status="incident", outcome="hotfix", why="an exposed secret",
                                    incident_id=touched[0] if touched else "")

    def _closed(self, rec: dict) -> dict:
        if rec.get("incident_id") or rec.get("raised"):
            self.engine.ingest([self._signal(rec, "resolved", rec["title"][:200], "The alert closed.", "sev3")],
                               background=True)
        return rec

    def _signal(self, rec: dict, status: str, title: str, detail: str, severity: str) -> dict:
        from ..ops.signals import now

        kind = "secret" if rec["kind"] == "secret" else "overdue"
        return {"source": "github-security", "category": "security", "status": status,
                "product_id": rec["product_id"], "component": "", "env": "production", "severity": severity,
                "title": title, "detail": detail[:4000], "fingerprint": f"upkeep:{kind}:{rec['id']}",
                "url": rec.get("url", ""), "at": now(), "no_correlate": True,
                "labels": {"finding": rec["id"], "repo": rec["repo"], "kind": rec["kind"]}}

    # ------------------------------------------------------------------ people

    def accept(self, finding_id: str, *, approver: str, reason: str, days: int) -> dict:
        rec = self.register.get(finding_id)
        if rec is None:
            raise KeyError(finding_id)
        if rec["status"] in CLOSED:
            raise ValueError(f"{finding_id} is already {rec['status']}")
        q = self.services.settings.quality
        limit = int(getattr(q, "waiver_max_days", 90) or 90)
        if not reason.strip():
            raise ValueError("Say why the risk is acceptable")
        if not 1 <= int(days) <= limit:
            raise ValueError(f"An accepted risk lasts 1 to {limit} days")
        expires = (_today() + timedelta(days=int(days))).isoformat()
        rec = self.register.update(finding_id, f"risk accepted by {approver} until {expires}: {reason}",
                                   status="accepted", accepted={"approver": approver, "reason": reason, "expires": expires})
        if rec.get("raised"):
            self.engine.ingest([self._signal(rec, "resolved", rec["title"][:200], f"Risk accepted until {expires}.",
                                             "sev3")], background=True)
            rec = self.register.update(finding_id, raised=False)
        return rec

    # ------------------------------------------------------------------ releases

    def fix_window(self, packet: dict) -> dict | None:
        """A held fix waits before G6: it joins the next release whose target date is on or before
        its release-by date, or, on that date, goes to its own G6. A finding that became urgent
        (re-triaged as a hotfix) ends the wait at once."""
        fid, pid = packet["feature_id"], packet["product_id"]
        by = packet.get("release_by") or ""
        recs = [r for r in self.register.list(pid) if r.get("fix_feature") == fid]
        if any(r.get("outcome") == "hotfix" for r in recs):
            return {"status": "due", "why": "a finding it fixes became urgent"}
        rel = self.joinable(pid, by)
        if rel:
            self.register.join(fid, rel["id"])
            for r in recs:
                self.register.update(r["id"], f"rides with {rel['id']} (target {rel['target_date']})", release_id=rel["id"])
            return {"status": "joined", "release": {"id": rel["id"], "model": rel["model"], "status": rel["status"]},
                    "why": f"rides with {rel['id']}, due {rel['target_date']}"}
        if not by or by <= _today().isoformat():
            return {"status": "due", "why": f"no release before {by}" if by else "no deadline to wait for"}
        return None

    def joinable(self, product_id: str, by: str) -> dict | None:
        """The earliest release still gathering its features (waiting at release_ready), with a
        target date on or before `by`."""
        best = None
        for row in self.engine._rows(product_id, "release"):
            rid = row["feature_id"]
            try:
                s = self.engine._state(rid)
                gathering = any(p.get("wait") == "release_ready" for p in self.engine.pending(rid))
            except Exception:
                continue
            target = str(s.get("target_date") or "")
            if not gathering or not target or (by and target > by) or target < _today().isoformat():
                continue
            if best is None or target < best["target_date"]:
                best = {"id": rid, "model": s.get("model", "launch"), "status": s.get("status", ""), "target_date": target}
        return best

    def past_release_by(self, feature_id: str) -> str:
        """Why a fix waiting with its release should leave it now, or ""."""
        try:
            h = self.engine._state(feature_id).get("hotfix") or {}
        except Exception:
            return ""
        by = h.get("release_by") or ""
        if h.get("source") == "finding" and by and by <= _today().isoformat():
            self.register.unjoin(feature_id)
            return f"its release-by date, {by}, came before the release was approved"
        return ""

    # ------------------------------------------------------------------ Dependabot

    def dependabot(self, product_id: str, repo: str | None = None) -> list[str]:
        """Dependabot's version updates merge by themselves when they are patch or minor (the
        policy's auto_merge) and every check is green: the tool posts the gates status and merges.
        A pull request for a package with an open finding is a security update: the finding's own
        fix handles it. Merged updates reach production with the next release."""
        gh = self.services.github
        pol = self.policy(product_id)
        allowed = set((pol.get("dependabot") or {}).get("auto_merge") or [])
        if not allowed:
            return []
        vulnerable = {r["package"].lower() for r in self.register.list(product_id)
                      if r["kind"] == "dependency" and r["state"] == "open" and r.get("package")}
        lane = set()
        try:
            lane = {r.name for r in self.services.snapshot(product_id).product.repos}
        except Exception:
            return []
        merged = []
        for name in sorted(lane):
            if repo and name != repo:
                continue
            try:
                prs = gh.open_pull_requests(repo=name) or []
            except Exception as exc:
                log.warning("dependabot: %s: %s", name, exc)
                continue
            for pr in prs:
                if pr.get("author") != "dependabot[bot]" or not str(pr.get("branch", "")).startswith("dependabot/"):
                    continue
                text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
                pkgs = {p.lower() for p in _BUMP_PKG.findall(text) + _UPDATES_PKG.findall(text)}
                if pkgs & vulnerable:
                    continue
                kinds = [update_type(a, b) for a, b in _BUMP.findall(text)]
                if not kinds or "" in kinds:
                    continue
                kind = max(kinds, key=lambda k: ("patch", "minor", "major").index(k))
                if kind not in allowed:
                    continue
                res = gh.checks(repo=name, sha=pr["head_sha"])
                mine = [c for c in res.get("checks", []) if c.get("name") != "valxel/gates"]
                if not mine or any(c.get("state") != "success" for c in mine):
                    continue
                try:
                    gh.set_status(repo=name, sha=pr["head_sha"], context="valxel/gates", state="success",
                                  description=f"Dependabot {kind} update, checks green: merged by the upkeep policy")
                    gh.merge(repo=name, number=pr["number"], actor=AGENT, sha=pr["head_sha"])
                    merged.append(f"{name}#{pr['number']}")
                    log.info("merged Dependabot %s update %s#%s", kind, name, pr["number"])
                except Exception as exc:
                    log.warning("couldn't merge %s#%s: %s", name, pr.get("number"), exc)
        return merged
