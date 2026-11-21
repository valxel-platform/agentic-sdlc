"""Production: signals, incidents, suspects, hotfixes and remediation."""

from __future__ import annotations

import logging



from langgraph.types import Command




class OperationsMixin:
    """FeatureEngine's operations methods (valxel_sdlc.engine)."""

    # --- operations: production signals and incidents --------------------------------------
    def ingest(self, signals: list[dict], background: bool = False) -> list[str]:
        """Attach each signal to the open incident it belongs to (same alert, issue or component),
        or open a new incident for a firing one, paging on-call. Returns the incidents touched.
        With background, triage runs after this returns (webhooks answer at once)."""
        import logging

        from ..ops import correlate
        from ..ops.signals import resolve_product, unmatched_reason

        touched: list[str] = []
        for sig in signals:
            product_id, repo = resolve_product(self.services, sig)
            if not product_id:
                # Kept, not dropped: a mislabelled alert source shows up in the unmatched list.
                reason = unmatched_reason(self.services, sig)
                logging.getLogger(__name__).warning("signal %s matched no product: %s", sig.get("fingerprint"), reason)
                keep = getattr(self.services.signals, "add_unmatched", None)
                if callable(keep):
                    keep(sig, reason)
                self.bus.publish({"type": "signal.unmatched", "fingerprint": sig.get("fingerprint", "")})
                continue
            sig = {**sig, "product_id": product_id, "component": repo or sig.get("component", "")}
            sig = self._with_traces(sig)
            joined, others = "", []
            with self._create_lock:
                # A cost finding joins only the incident for the same finding, never a component's outage.
                match = "" if sig.get("category") == "cost" else repo
                iid = self.services.signals.open_incident_for(product_id, sig["fingerprint"], match)
                created = False
                if sig["status"] == "firing" and sig.get("category") != "cost" and not sig.get("no_correlate"):
                    # The same failure seen from another component (ops/correlate.py). A signal that
                    # names the same request as other open incidents ties them into one.
                    matches = correlate.related(self, product_id, sig)
                    if iid is None and matches:
                        iid, joined = matches[0]
                        sig = {**sig, "labels": {**(sig.get("labels") or {}), "joined_by": joined}}
                    others = [m for m, why in matches if m != iid and why.startswith("trace")]
                if iid is None and sig["status"] == "firing":
                    iid = self._new_id_locked(product_id, "incident", sig["title"])
                    created = True
                if iid is None:
                    continue  # a resolution for an incident already closed
                self.services.signals.add(iid, product_id, sig)
            for other in others:  # this signal ties them together: one incident
                self.merge_incidents(other, iid, approver="agent:correlate",
                                     reason=f"{sig['title']} ties it to {iid} ({joined})")
            if joined:
                self._root_moved(iid)
            if created:
                inp = {"incident_id": iid, "product_id": product_id, "title": sig["title"], "severity": sig["severity"],
                       "component": repo, "opened_at": sig["at"], "status": "triaging",
                       "events": [self._page(iid, product_id, repo, sig)]}
                self.bus.publish({"type": "feature.created", "feature_id": iid, "product_id": product_id})
                (self._submit if background else self._execute)(iid, inp)
            else:
                (self.poke if background else self.poke_now)(iid)  # a resolution may end its recovery wait
            touched.append(iid)
        return list(dict.fromkeys(touched))

    def _page(self, iid: str, product_id: str, repo: str, sig: dict) -> dict:
        """A new incident: tell on-call (low urgency) if its severity calls for a heads-up while the
        tool works on it (platform/remediation.yaml page.heads_up). Otherwise nobody is paged
        unless the tool can't fix it (ops/incident.py escalate)."""
        from ..roadmap.graphs import event

        try:
            snap = self.services.snapshot(product_id)
            data_class, heads_up = snap.product.data_class, (snap.remediation.get("page") or {}).get("heads_up", ["sev1"])
        except Exception:
            data_class, heads_up = "phi", ["sev1"]  # unknown: say as little as possible
        if sig.get("category") == "cost" or sig["severity"] not in heads_up:
            return event("page", "not_paged", "the tool is working on it; on-call is paged only if it can't fix it")
        try:
            said = self.services.pager.trigger({
                "incident_id": iid, "product_id": product_id, "title": sig["title"], "severity": sig["severity"],
                "component": repo or sig.get("component", ""), "env": sig.get("env", "production"),
                "source": sig.get("source", ""), "data_class": data_class})
        except Exception as exc:
            said = f"not paged: {type(exc).__name__}: {exc}"
        return event("page", "not_paged" if said.startswith("not paged") else "paged", said)

    def error_lines(self, product_id: str, component: str, minutes: int = 5) -> int | None:
        """Error lines in a production component's recent logs, or None when they can't be read."""
        try:
            rt = self.services.snapshot(product_id).product.runtime or {}
            got = self.services.openchoreo.observe(
                component=(rt.get("components") or {}).get(component, component), env="production",
                project=rt.get("openchoreo_project"), namespace=rt.get("namespace"), minutes=minutes)
        except Exception:
            return None
        if not got.get("available", True):
            return None
        return sum(1 for line in got.get("logs") or [] if "stack_trace" in line or '"ERROR"' in line)

    def unmatched_signals(self, limit: int = 100) -> list[dict]:
        """Signals that matched no product, newest first, with why (ops/signals.py unmatched_reason)."""
        lister = getattr(self.services.signals, "unmatched", None)
        return lister(limit) if callable(lister) else []

    def alarm_changes(self, signals: list[dict], background: bool = False) -> dict:
        """CloudWatch alarm state changes: every one goes on the alarm board (what G6 reads);
        production ones also go into the incident flow."""
        from ..ops.signals import resolve_product

        kept, production = 0, []
        for sig in signals:
            product_id, _ = resolve_product(self.services, sig)
            if not product_id:
                continue
            sig = {**sig, "product_id": product_id}
            self.services.alarms.record(sig)
            kept += 1
            if sig["env"] == "production":
                production.append(sig)
        return {"recorded": kept, "incidents": self.ingest(production, background=background) if production else []}

    # --- correlation: one incident per failure ------------------------------------------------
    def _with_traces(self, sig: dict) -> dict:
        """An OpenChoreo log alert names no request: read the trace ids of the component's recent
        error logs, so it can be matched to the apps' and other services' reports (best effort)."""
        from ..ops.correlate import traces_in_logs

        labels = sig.get("labels") or {}
        if sig.get("source") != "openchoreo" or labels.get("alertType") != "log" or labels.get("trace_ids"):
            return sig
        try:
            snap = self.services.snapshot(sig["product_id"])
            rt = snap.product.runtime or {}
            got = self.services.openchoreo.observe(
                component=(rt.get("components") or {}).get(sig["component"], sig["component"]), env=sig.get("env", "production"),
                project=rt.get("openchoreo_project"), namespace=rt.get("namespace"), minutes=10)
        except Exception:
            return sig
        ids = traces_in_logs(got.get("logs") or [])
        return {**sig, "labels": {**labels, "trace_ids": ",".join(ids)}} if ids else sig

    def _root_moved(self, incident_id: str) -> None:
        """A joined signal may show the failure starts further downstream: if the incident is
        waiting for on-call, triage again around the new root cause candidate."""
        from ..ops.correlate import root

        s = self._state(incident_id)
        if not s or s.get("status") != "awaiting_decision":
            return
        cand, why = root(self.services.snapshot(s["product_id"]), self.services.signals.for_incident(incident_id),
                         s.get("component", ""))
        if not cand or cand == s.get("component"):
            return
        item = next((p for p in self.pending(incident_id) if p.get("gate") == "incident"), None)
        if item:
            try:
                self.decide(incident_id, item["interrupt_id"], {
                    "decision": "revise", "approver": "agent:correlate",
                    "feedback": f"A related signal joined; the root cause candidate is now {cand}: {why}"})
            except RuntimeError:
                pass  # running: its next triage reads the signals anyway

    def merge_incidents(self, incident_id: str, into: str, *, approver: str, reason: str = "") -> dict:
        """Move one incident's signals into another and end it as merged. On-call can do this; the
        tool does it when a new signal ties two open incidents together."""
        if incident_id == into:
            raise ValueError("An incident can't be merged into itself")
        moved = self.services.signals.move(incident_id, into)
        self.merged[incident_id] = into
        pending = self.pending(incident_id)
        item = next((p for p in pending if p.get("gate") == "incident"), None)
        wait = next((p for p in pending if p.get("wait") == "incident_recovery"), None)
        ended = False
        if item:
            self.decide(incident_id, item["interrupt_id"], {"decision": "merge", "into": into, "approver": approver,
                                                            "feedback": reason or f"Merged into {into}"})
            ended = True
        elif wait and not self.is_running(incident_id):  # the tool was watching it recover
            self.services.close_incident(incident_id)
            self._run(incident_id, Command(resume={wait["interrupt_id"]: {"status": "merged", "into": into}}))
            ended = True
        else:  # busy: it ends at its next step (ops/incident.py merged)
            self.services.close_incident(incident_id)
        product_id = self._state(incident_id).get("product_id", "")
        self.services.pager.resolve(incident_id, product_id, f"merged into {into}")
        self._root_moved(into)
        self.bus.publish({"type": "feature.updated", "feature_id": into})
        return {"merged": incident_id, "into": into, "signals": moved, "ended": ended}

    def split_signal(self, incident_id: str, fingerprint: str, *, approver: str) -> dict:
        """Take a signal that was joined wrongly out of an incident: it opens its own."""
        sigs = [s for s in self.services.signals.for_incident(incident_id) if s["fingerprint"] == fingerprint]
        if not sigs:
            raise KeyError(f"{incident_id} has no signal {fingerprint}")
        latest = dict(sigs[-1])
        latest.pop("incident_id", None)
        placeholder = f"{incident_id}:split:{fingerprint}"
        self.services.signals.move(incident_id, placeholder, fingerprint)
        self.services.signals.close(placeholder)
        labels = {k: v for k, v in (latest.get("labels") or {}).items() if k != "joined_by"}
        new = self.ingest([{**latest, "status": "firing", "no_correlate": True,
                            "labels": {**labels, "split_from": incident_id, "split_by": approver}}])
        self.bus.publish({"type": "feature.updated", "feature_id": incident_id})
        return {"incident_id": incident_id, "signal": fingerprint, "new_incident": new[0] if new else None}

    def incident_summaries(self, product_id: str, limit: int = 20) -> list[dict]:
        out = []
        for row in reversed(self._rows(product_id, "incident")):
            s = self._state(row["feature_id"])
            cause = ""
            from ..ops.incident import section

            cause = section(s.get("triage", ""), "Likely cause").split("\n")[0][:300]
            out.append({"id": row["feature_id"], "title": s.get("title") or row["title"], "severity": s.get("severity"),
                        "status": s.get("status"), "component": s.get("component", ""), "cause": cause,
                        "links": s.get("links") or {}})
            if len(out) >= limit:
                break
        return out

    # --- traceability: from a feature's reason to production, and back -------------------------
    def trace(self, feature_id: str) -> dict:
        """The feature's trace: metrics, goals, criteria, tasks, PRs, acceptance results,
        deployments and the production incidents on its components (valxel_sdlc.traceability)."""
        from .. import traceability

        s = self._state(feature_id)
        if not s:
            raise KeyError(f"Unknown feature {feature_id}")
        arts = s.get("artifacts") or {}

        def load(kind: str) -> str:
            from ..drafts import load as read

            try:
                return read(self.services, {**s, "feature_id": feature_id}, kind) if kind in arts else ""
            except Exception:
                return ""

        components = sorted({r for r, d in (s.get("deployments") or {}).items()
                             if d.get("env") and d.get("kind") != "resource"})
        incidents = [{"id": i.get("feature_id", ""), "title": i.get("title", ""), "status": i.get("status", ""),
                      "severity": i.get("severity", ""), "touch": i.get("touch", "")}
                     for i in (self._touching(s["product_id"], components) if components else [])]
        return traceability.build(s, load, incidents)

    def trace_search(self, product_id: str, *, repo: str = "", pr: str = "") -> list[dict]:
        """Which of a product's features touched a repo or a pull request, and through which criteria."""
        from .. import traceability

        rows = [{"feature_id": r["feature_id"], "title": r["title"], "state": self._state(r["feature_id"])}
                for r in self.index.all()
                if r["product_id"] == product_id and r.get("kind", "feature") == "feature"]
        return traceability.search(rows, repo=repo, pr=pr)

    def suspects(self, product_id: str, repo: str = "") -> list[dict]:
        """Released features that changed the component, most recent first (all recent ones when the
        component is unknown): the first places triage looks. Features of the products built on
        this one count too (they can change its repos and they call its components).

        A feature still rolling out (its flag on for some users) is a suspect too when it changed a
        component that calls the failing one or that the failing one calls, so the incident can
        turn its flag off: a broken provider shows up as errors in its callers, the web app
        included, and a caller's new traffic can break what it calls."""
        from ..ops.correlate import graph_for, short

        products = [product_id]
        if repo:
            for pid in self.services.settings.registry:
                if pid == product_id:
                    continue
                try:
                    if product_id in self.services.snapshot(pid).product.depends_on:
                        products.append(pid)
                except Exception:
                    continue
        graphs: dict = {}
        out = []
        for pid in products:
            for row in self._rows(pid, "feature"):
                s = self._state(row["feature_id"])
                rel = s.get("release") or {}
                if not rel.get("production"):
                    continue
                repos = set((s.get("change_set") or {}).get("merge_order") or []) | \
                    {pr["repo"] for pr in (s.get("pull_requests") or {}).values()}
                via = ""
                if repo and repo not in repos:
                    rolling = bool(rel.get("rollout_plan")) and 0 < float(rel.get("flag_percent") or 0) < 100
                    if not rolling:
                        continue
                    if pid not in graphs:
                        try:
                            graphs[pid] = graph_for(self.services.snapshot(pid))
                        except Exception:
                            graphs[pid] = None
                    if graphs[pid] is None or not any(graphs[pid].related(r, repo) for r in repos):
                        continue
                    via = (f"rolling out to {float(rel.get('flag_percent')):g}% in a component that {short(repo)} "
                           f"calls or is called by")
                at = next((g["at"] for g in reversed(s.get("gate_decisions") or []) if g["gate"] == "g6"), "")
                extra: dict = {"via": via} if via else {}
                if pid != product_id:
                    # Another product's flag: the tool turns it off by itself only if that product lets it.
                    try:
                        mine = "flag_off" in ((self.services.snapshot(pid).remediation or {}).get("autonomous") or {})
                    except Exception:
                        mine = False
                    extra |= {"product_id": pid, "flag_off_allowed": mine}
                out.append({"feature_id": row["feature_id"], "title": s.get("title", ""), "released_at": at,
                            "flag": rel.get("flag", ""), "flag_on": self._flag_on(s), "repos": sorted(repos),
                            "track": s.get("track", "standard"), **extra})
        return sorted(out, key=lambda x: x["released_at"], reverse=True)

    def live_features(self, product_id: str) -> dict[str, dict]:
        """Features in production of this product and the products built on it, with what undoing
        each needs (ops/undo.py): its flag, what depends on it, whether its data can go back."""
        products = [product_id]
        for pid in self.services.settings.registry:
            try:
                if pid != product_id and product_id in self.services.snapshot(pid).product.depends_on:
                    products.append(pid)
            except Exception:  # a product whose governance can't be read adds nothing
                logging.getLogger(__name__).debug("%s: can't read its governance", pid, exc_info=True)
                continue
        out = {}
        for pid in products:
            for row in self._rows(pid, "feature"):
                s = self._state(row["feature_id"])
                rel = s.get("release") or {}
                if not rel.get("production"):
                    continue
                u = s.get("undo") or {}
                repos = set((s.get("change_set") or {}).get("merge_order") or []) | \
                    {pr["repo"] for pr in (s.get("pull_requests") or {}).values()}
                r = self.release_for(row["feature_id"])
                out[row["feature_id"]] = {
                    "title": s.get("title", ""), "flag": rel.get("flag", ""), "flag_on": self._flag_on(s),
                    "depends_on": list(u.get("depends_on") or []), "flag_off": u.get("flag_off", ""),
                    "introduces": u.get("introduces") or {}, "uses": u.get("uses") or {},
                    "rollback": u.get("rollback") or {}, "repos": sorted(repos),
                    "release": {"id": r["id"], "size": len(self.release_members(r["id"]))} if r else {}}
        return out

    def turned_off(self, product_id: str) -> dict[str, str]:
        """Features an incident turned off in production: feature id -> the incident (latest wins)."""
        out: dict[str, str] = {}
        for row in self._rows(product_id, "incident"):
            for t in self._state(row["feature_id"]).get("taken") or []:
                if t.get("type") == "flag_off" and t.get("ok") and t.get("feature_id"):
                    out[t["feature_id"]] = row["feature_id"]
        return out

    def undo_plan(self, product_id: str, actions: list[dict], component: str = "") -> list[dict]:
        from ..undo import derived_from, plan

        live = self.live_features(product_id)
        # Dependents from what each design uses, not only from its G4 list: a feature on for everyone
        # when another was designed still takes that one off with it (T-110).
        intro = [{"feature_id": f, "title": i["title"], "introduces": i.get("introduces") or {}} for f, i in live.items()]
        for f, i in live.items():
            more = derived_from(i.get("uses") or {}, [o for o in intro if o["feature_id"] != f])
            i["depends_on"] = list(dict.fromkeys([*i["depends_on"], *more]))
        return plan(actions, live, component)

    def start_hotfix(self, incident: dict, *, repo: str, change: str, approver: str) -> str:
        if not repo:
            raise ValueError("A hotfix needs the repo to change")
        return self.create(product_id=incident["product_id"], title=f"Hotfix: {change}"[:150],
                           description=f"Fix for incident {incident['incident_id']} ({incident['severity']}): "
                                       f"{incident['title']}.\n\n{change}",
                           created_by=approver, background=True, track="hotfix",
                           hotfix={"incident_id": incident["incident_id"], "repo": repo, "change": change,
                                   "severity": incident["severity"], "approved_by": approver})

    def start_intent_change(self, incident: dict, *, scope: str, intent: str, approver: str) -> str:
        signals = self.services.signals.for_incident(incident["incident_id"])
        evidence = "\n".join(f"- {s['source']}: {s['title']}" for s in signals[:10])
        request = (f"{intent}\n\nRaised by incident {incident['incident_id']} ({incident['severity']}): "
                   f"{incident['title']}.\nWhat users reported or what failed:\n{evidence}")
        if scope == "initiative":
            return self.create_initiative(product_id=incident["product_id"], title=intent[:120], request=request,
                                          created_by=approver, background=True)
        suspects = self.suspects(incident["product_id"], incident.get("component", ""))
        initiative = next((self._state(x["feature_id"]).get("initiative_id") for x in suspects
                           if self._state(x["feature_id"]).get("initiative_id")), "")
        return self.create(product_id=incident["product_id"], title=intent[:150], description=request,
                           created_by=approver, background=True, initiative_id=initiative or "",
                           not_duplicate_reason=f"Changes what was built, after incident {incident['incident_id']}")

    def _cohorts(self, product_id: str, flags: list[str], packet: dict) -> dict:
        """Users with each flag on against off, since the step began, per component and per app
        (ops/cohorts.py): {flag: {group: comparison}}. The whole project's summary lines count, not
        only the feature's components: anything that checks the flag is watched. Empty when there's
        no log source or no summary lines (a UI-only feature on an older template)."""
        from ..ops import cohorts as co
        from ..ops.rollout import _at, _clock

        logs = getattr(self.services, "cohort_logs", None)
        if logs is None:
            return {}
        try:
            rt = self.services.snapshot(product_id).product.runtime or {}
            since = _at(packet["since"]) or _clock()
            entries = logs.entries(namespace=rt.get("namespace") or "default", project=rt.get("openchoreo_project") or "",
                                   environment=list(rt.get("pipeline") or ["production"])[-1], since=since, until=_clock())
        except Exception:
            return {}
        out: dict[str, dict] = {}
        parsed = co.parse_entries(entries, flags)
        for k, groups in parsed.items():
            got = {g: {**co.compare(c, packet.get("compare")), "group": g} for g, c in groups.items() if c["lines"]}
            if got:
                out[k] = got
        # The feature's goal events, per side (outcome loop, step 4): reported, never a reason to stop.
        events = list(packet.get("goal_events") or [])
        key = packet.get("flag")
        if events and key in parsed and any(c["lines"] for c in parsed[key].values()):
            out["__goal__"] = co.goal_events(parsed[key], events, packet.get("goal_compare"))
        return out

    def _touching(self, product_id: str, components) -> list[dict]:
        """Incidents a rollout of these components should wait out: in one of them, or in a component
        that calls them or that they call (the call graph, across the lanes the product builds on),
        web and mobile apps included.
        A feature that breaks a provider shows up first as errors in its callers."""
        from ..ops.correlate import graph_for, short

        comps = {short(c) for c in components or [] if short(c)}
        if not comps:
            return []
        try:
            snap = self.services.snapshot(product_id)
            graph, lanes = graph_for(snap), list(snap.product.depends_on)
        except Exception:
            graph, lanes = None, []
        out = []
        # The lanes it builds on too: their components are called by this product's.
        for row in [r for pid in [product_id, *lanes] for r in self._rows(pid, "incident")]:
            s = self._state(row["feature_id"])
            theirs = {short(s.get("component", ""))} | {short(x.get("component") or "")
                                                         for x in self.services.signals.for_incident(row["feature_id"])}
            theirs.discard("")
            if comps & theirs:
                out.append({**s, "touch": "same component"})
            elif graph is not None and any(graph.related(a, b) for a in comps for b in theirs):
                out.append({**s, "touch": "calls or is called by it"})
        return out

    def feature_info(self, feature_id: str) -> dict:
        """A feature's risk tier and production components (repos), for a launch's gradual release."""
        s = self._state(feature_id)
        comps = sorted({d.get("repo") for d in (s.get("deployments") or {}).values()
                        if d.get("env") == "production" and d.get("kind") not in ("resource", "store") and d.get("repo")})
        return {"risk_tier": s.get("risk_tier"), "components": comps}

    def shadow_record(self, window_days: int = 90) -> dict:
        """How triage's shadow choices compared with what fixed each incident, across all products
        (ops/remediation.py shadow mode): triage is one config for all of them."""
        from ..ops.remediation import shadow_record

        rows = [r for r in self.index.all() if r.get("kind") == "incident"]
        return shadow_record([self._state(r["feature_id"]) for r in rows], window_days)

    def remediation_record(self, product_id: str, window_days: int = 90) -> dict:
        """How the tool's own fixes went for a product (ops/remediation.py calibration)."""
        from ..ops.remediation import track_record

        return track_record([self._state(r["feature_id"]) for r in self._rows(product_id, "incident")], window_days)
