"""Initiatives, inceptions, releases, outcome reviews and change requests."""

from __future__ import annotations

import logging

from datetime import datetime, timezone


from .base import kind_of

log = logging.getLogger(__name__)


class RoadmapMixin:
    """FeatureEngine's roadmap methods (valxel_sdlc.engine)."""

    def collect_outcomes(self, product_id: str | None = None) -> list[dict]:
        """Readouts that are due, each goal's weekly trend reading (valxel_sdlc.outcomes.readouts),
        then the initiative reviews that are due (outcomes/review.py)."""
        from ..outcomes import readouts

        taken = readouts.run(self, product_id)
        for pid in [product_id] if product_id else sorted({r["product_id"] for r in self.index.all()}):
            try:
                self.start_due_reviews(pid)
            except Exception:
                continue
        return taken

    def review_summaries(self, product_id: str) -> list[dict]:
        out = []
        for row in self._rows(product_id, "review"):
            s = self._state(row["feature_id"])
            out.append({"id": row["feature_id"], "initiative_id": s.get("initiative_id", ""), "title": s.get("title") or row["title"],
                        "status": s.get("status", "preparing"), "why": s.get("why", ""), "opened": s.get("opened") or []})
        return out

    def create_review(self, *, product_id: str, initiative_id: str, why: str, created_by: str = "valxel",
                      background: bool = False) -> str:
        """Ask the product council whether an initiative should continue, change course or stop."""
        open_ = [r["id"] for r in self.review_summaries(product_id)
                 if r["initiative_id"] == initiative_id and r["status"] in ("preparing", "waiting")]
        if open_:
            return open_[0]
        from ..outcomes.review import initiative_of

        i = initiative_of(self.services, product_id, initiative_id)
        tid = self._new_id(product_id, "review", f"Review: {i['title']}")
        inp = {"review_id": tid, "product_id": product_id, "initiative_id": initiative_id,
               "title": f"Review: {i['title']}", "why": why, "created_by": created_by, "status": "preparing"}
        self.bus.publish({"type": "feature.created", "feature_id": tid, "product_id": product_id})
        (self._submit if background else self._execute)(tid, inp)
        return tid

    def start_due_reviews(self, product_id: str, today=None) -> list[str]:
        from ..outcomes import review
        from ..outcomes.readouts import product_outcomes
        from ..roadmap import read_roadmap

        road = read_roadmap(self.services, product_id)
        outcomes = product_outcomes(self, product_id, road)
        return [self.create_review(product_id=product_id, initiative_id=i["id"], why=why, background=True)
                for i, why in review.due(self.services, product_id, road, outcomes, today)]

    def initiative_summaries(self, product_id: str) -> list[dict]:
        out = []
        for row in self._rows(product_id, "initiative"):
            s = self._state(row["feature_id"])
            d = s.get("draft") or {}
            out.append({"id": row["feature_id"], "title": s.get("title") or row["title"],
                        "status": s.get("status", "drafting"), "outcome": d.get("outcome", s.get("request", "")),
                        "goal": d.get("goal") or ((d.get("new_goal") or {}).get("title", "")), "horizon": d.get("horizon", ""),
                        "budget_usd": d.get("budget_usd", 0.0), "lanes": d.get("lanes", []),
                        "release_model": d.get("release_model", "launch"), "candidates": d.get("candidates", []),
                        "record_pr": (s.get("record_pr") or {}).get("url", "")})
        return out

    def create_initiative(self, *, product_id: str, title: str, request: str, created_by: str = "",
                          measure: str = "", horizon: str = "", background: bool = False,
                          research: str = "quick") -> str:
        self.services.snapshot(product_id)  # validates the product's governance
        iid = self._new_id(product_id, "initiative", title)
        inp = {"initiative_id": iid, "product_id": product_id, "title": title, "request": request,
               "created_by": created_by, "measure_hint": measure, "horizon_hint": horizon, "attempt": 0,
               "research_depth": research}
        self.bus.publish({"type": "feature.created", "feature_id": iid, "product_id": product_id})
        (self._submit if background else self._execute)(iid, inp)
        return iid

    def resize(self, thread_id: str, *, by: str, background: bool = False) -> str:
        """A feature still at its first gate becomes an initiative, or an initiative not yet approved
        becomes one feature. What was written carries over; the old thread stops, saying where it went."""
        state = self._state(thread_id)
        kind = kind_of(thread_id)
        gate = {"feature": "g1", "initiative": "g0"}.get(kind)
        if gate is None:
            raise ValueError("Only a feature or an initiative can change size")
        waiting = [p for p in self.pending(thread_id) if p.get("gate") == gate]
        if not waiting:
            raise ValueError(f"{thread_id} can change size only while it waits at {gate.upper()}, before it's approved")
        if kind == "feature":
            new = self.create_initiative(product_id=state["product_id"], title=state["title"],
                                         request=state.get("description") or state["title"], created_by=by,
                                         background=background, research="quick")
        else:
            new = self.create(product_id=state["product_id"], title=state["title"], description=state.get("request", ""),
                              created_by=by, background=background,
                              not_duplicate_reason=f"Was initiative {thread_id}")
        self.decide(thread_id, waiting[0]["interrupt_id"],
                    {"decision": "abort", "approver": by,
                     "feedback": f"Became {'initiative' if kind == 'feature' else 'feature'} {new}"},
                    background=background)
        return new

    def change_request(self, feature_id: str, description: str, *, by: str) -> dict:
        """The person who asked changes what they asked for, before the build starts. The new request
        replaces the old; a gate waiting now goes back to its agents with the change as feedback."""
        import difflib as _difflib

        s = self._state(feature_id)
        if (s.get("approvals") or {}).get("g4"):
            raise ValueError("The build has started: start a follow-up feature for the change instead")
        if s.get("status") in ("done", "aborted"):
            raise ValueError("This feature has finished")
        old = s.get("description", "")
        if description.strip() == old.strip():
            raise ValueError("Nothing changed")
        if self.is_running(feature_id):
            raise RuntimeError("Agents are working on it right now: try again when it's waiting")
        added = [ln[2:] for ln in _difflib.ndiff(old.splitlines(), description.splitlines()) if ln.startswith("+ ")]
        removed = [ln[2:] for ln in _difflib.ndiff(old.splitlines(), description.splitlines()) if ln.startswith("- ")]
        note = ("The person who asked for this changed the request."
                + (" Added: " + " / ".join(added) if added else "") + (" Removed: " + " / ".join(removed) if removed else ""))
        _now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

        change = {"description": description.strip(),
                  "events": [{"at": _now(), "node": "request", "kind": "changed", "detail": note[:500], "by": by}]}
        gate = next((p for p in self.pending(feature_id) if p.get("type") == "gate" and "revise" in (p.get("allowed") or [])), None)
        if gate:  # the new request and the send-back in one step, so the agents revise against it
            self.decide(feature_id, gate["interrupt_id"], {"decision": "revise", "approver": by, "feedback": note,
                                                           "reason": "wrong_scope"}, background=True, update=change)
        else:
            self._g(feature_id).update_state(self._config(feature_id), change)
        return {"feature_id": feature_id, "sent_back": gate.get("gate") if gate else "", "note": note}

    # ------------------------------------------------------------------ no dead ends after approval

    def record_prs(self, thread_id: str) -> list[dict]:
        """The governance pull requests an approval opened, and whether each is merged yet."""
        import re as _re

        s = self._state(thread_id)
        out = []
        if s.get("record_pr", {}).get("url"):
            pr = s["record_pr"]
            out.append({"key": "record", "label": "Roadmap record", "url": pr["url"], "merged": bool(pr.get("merged")),
                        "repo": self.services.settings.entry(s["product_id"]).governance.full_name,
                        "number": pr.get("number") or int(_re.search(r"/pull/(\d+)", pr["url"]).group(1))})
        for st in (s.get("setup") or {}).get("steps") or []:
            if st.get("url") and st["step"] in ("governance_pr", "registry_pr"):
                out.append({"key": st["step"], "url": st["url"], "merged": bool(st.get("merged")), "repo": st["repo"],
                            "label": "Governance" if st["step"] == "governance_pr" else "Registry",
                            "number": int(_re.search(r"/pull/(\d+)", st["url"]).group(1))})
        return out

    def merge_record(self, thread_id: str, key: str, *, by: str) -> dict:
        """Merge one of those pull requests from the tool: the person approved it already, so no second
        click on GitHub. Branch protection still applies; GitHub's refusal is passed on."""
        pr = next((p for p in self.record_prs(thread_id) if p["key"] == key), None)
        if pr is None:
            raise KeyError(f"{thread_id} has no {key} pull request")
        if not pr["merged"]:
            res = self.services.github.merge(repo=pr["repo"], number=pr["number"], actor=by)
            if res.get("state") != "merged":
                raise RuntimeError(f"GitHub didn't merge {pr['url']}")
        s = self._state(thread_id)
        if key == "record":
            update = {"record_pr": {**s["record_pr"], "merged": True, "merged_by": by}}
        else:
            setup = dict(s.get("setup") or {})
            setup["steps"] = [{**st, "merged": True, "merged_by": by} if st["step"] == key else st
                              for st in setup.get("steps") or []]
            update = {"setup": setup}
        self._g(thread_id).update_state(self._config(thread_id), update)
        self.bus.publish({"type": "feature.updated", "feature_id": thread_id})
        return {**pr, "merged": True}

    def _queue(self) -> dict:
        import json as _json
        from pathlib import Path as _P

        p = _P(self.services.settings.artifact_dir) / "start-queues.json"
        try:
            return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            return {}

    def _save_queue(self, q: dict) -> None:
        import json as _json
        from pathlib import Path as _P

        (_P(self.services.settings.artifact_dir) / "start-queues.json").write_text(_json.dumps(q), encoding="utf-8")

    def start_candidates(self, initiative_id: str, indexes: list[int], *, order: str, by: str) -> dict:
        """Start an approved initiative's candidate features: together, or in order (the next starts
        when the one before reaches everyone)."""
        v = self.view(initiative_id)
        if v["status"] != "approved":
            raise ValueError("Only an approved initiative's features can be started")
        cands = v["candidates"]
        picked = [i for i in dict.fromkeys(indexes) if 0 <= i < len(cands) and not cands[i]["started"]]
        if not picked:
            raise ValueError("Nothing to start: those features have started already")
        first = picked if order == "together" else picked[:1]
        started = [self.create(product_id=v["product_id"], title=cands[i]["title"], description=cands[i]["description"],
                               created_by=by, background=True, initiative_id=initiative_id,
                               not_duplicate_reason=f"A candidate feature of {initiative_id}") for i in first]
        q = self._queue()
        if order == "sequence" and len(picked) > 1:
            q[initiative_id] = {"after": started[-1], "next": picked[1:], "by": by}
        else:
            q.pop(initiative_id, None)
        self._save_queue(q)
        return {"started": started, "queued": [cands[i]["title"] for i in picked[1:]] if order == "sequence" else []}

    def _start_next_in_order(self, feature_id: str) -> str | None:
        """A feature that reached everyone: start the next one its initiative queued after it."""
        q = self._queue()
        hit = next(((iid, e) for iid, e in q.items() if e.get("after") == feature_id), None)
        if hit is None:
            return None
        iid, e = hit
        s = self._state(feature_id)
        if s.get("status") != "done":
            return None
        try:
            v = self.view(iid)
            nxt = next((i for i in e["next"] if i < len(v["candidates"]) and not v["candidates"][i]["started"]), None)
            if nxt is None:
                q.pop(iid)
                self._save_queue(q)
                return None
            c = v["candidates"][nxt]
            fid = self.create(product_id=v["product_id"], title=c["title"], description=c["description"],
                              created_by=e.get("by", ""), background=True, initiative_id=iid,
                              not_duplicate_reason=f"A candidate feature of {iid}, started in order")
        except Exception:
            log.warning("starting the next feature of %s failed", iid, exc_info=True)
            return None
        rest = [i for i in e["next"] if i != nxt]
        if rest:
            q[iid] = {**e, "after": fid, "next": rest}
        else:
            q.pop(iid)
        self._save_queue(q)
        return fid

    def start_first_feature(self, inception_id: str, *, by: str) -> str:
        """After inception: the first initiative's first feature, once the product is registered."""
        from ..inception import formats
        from ..inception.setup import ids

        s = self._state(inception_id)
        if s.get("status") != "set_up":
            raise ValueError("The product isn't set up yet: approve the product gate first")
        pid = ids(s["brief"])["product_id"]
        if pid not in self.services.settings.registry:
            raise ValueError(f"{pid} isn't registered yet: merge the governance and registry pull requests, then "
                             "reload the config")
        inis = [i for i in formats.initiatives(s.get("roadmap", "")) if i.get("id")]
        if not inis or not inis[0]["features"]:
            raise ValueError("The roadmap has no first feature to start")
        f = inis[0]["features"][0]
        return self.create(product_id=pid, title=f["title"], description=f.get("does", ""), created_by=by,
                           background=True, initiative_id=f"{pid}-INI-1")

    def create_inception(self, vision, *, created_by: str = "", background: bool = False) -> str:
        """Start a product inception: the brief first, then the product council's brief gate."""
        from dataclasses import asdict

        from ..inception.graph import PRODUCT_ID

        tid = self._new_id(PRODUCT_ID, "inception", vision.name)
        inp = {"inception_id": tid, "product_id": PRODUCT_ID, "title": vision.name, "vision": asdict(vision),
               "created_by": created_by, "status": "researching"}
        self.bus.publish({"type": "feature.created", "feature_id": tid, "product_id": PRODUCT_ID})
        (self._submit if background else self._execute)(tid, inp)
        return tid

    def inception_summaries(self) -> list[dict]:
        from ..inception.graph import PRODUCT_ID

        out = []
        for row in self._rows(PRODUCT_ID, "inception"):
            s = self._state(row["feature_id"])
            v = s.get("vision") or {}
            out.append({"id": row["feature_id"], "title": s.get("title") or row["title"], "org": v.get("org", ""),
                        "existing": bool(v.get("existing")), "product_id": v.get("product_id", ""),
                        "status": s.get("status", "drafting_brief"), "created_by": s.get("created_by", ""),
                        "cost_usd": round(float(s.get("cost_usd", 0.0)), 4),
                        "running": self.is_running(row["feature_id"])})
        return out

    def approved_initiative(self, product_id: str, initiative_id: str) -> dict | None:
        """An initiative features may link to: approved at G0 (record merged or still a PR)."""
        for i in self.initiative_summaries(product_id):
            if i["id"] == initiative_id:
                return i if i["status"] == "approved" else None
        from ..roadmap import read_roadmap

        try:
            for i in read_roadmap(self.services, product_id)["initiatives"]:
                if i["id"] == initiative_id and i["status"] in ("approved", "active"):
                    return i
        except Exception:
            return None
        return None

    def roadmap_fit(self, state: dict) -> dict:
        """For G1: the initiative a feature serves and what is left of its envelope, or 'unplanned'."""
        iid = state.get("initiative_id")
        if not iid:
            return {"unplanned": True}
        i = self.approved_initiative(state["product_id"], iid) or {"id": iid, "title": iid}
        spent = sum(f["cost_usd"] for f in self.feature_summaries(state["product_id"]) if f.get("initiative_id") == iid)
        envelope = float(i.get("budget_usd") or 0)
        return {"initiative": {k: i.get(k) for k in ("id", "title", "goal", "horizon", "release_model")},
                "budget_usd": envelope, "spent_usd": round(spent, 2), "remaining_usd": round(envelope - spent, 2),
                "over": bool(envelope) and spent > envelope}

    def release_for(self, feature_id: str, *, active: bool = False) -> dict | None:
        """The latest release a feature was put in. Cancelled ones count (a feature's G6 replays the
        same steps on resume), unless `active` asks for releases still under way."""
        product_id = feature_id.split("-", 1)[0]
        found = None
        for row in self._rows(product_id, "release"):
            s = self._state(row["feature_id"])
            if feature_id in (s.get("features") or []) and not (active and s.get("status") == "cancelled"):
                found = {"id": row["feature_id"], "model": s.get("model", "launch"), "status": s.get("status", "")}
        if found is None:  # a security fix that joined a release after it was planned (valxel_sdlc.upkeep)
            rid = self.services.findings.release_of(feature_id) if getattr(self.services, "findings", None) else None
            if rid:
                s = self._state(rid)
                if not (active and s.get("status") == "cancelled"):
                    found = {"id": rid, "model": s.get("model", "launch"), "status": s.get("status", "")}
        return found

    def create_release(self, *, product_id: str, title: str, model: str, features: list[str], target_date: str = "",
                       initiative: str = "", created_by: str = "", background: bool = False) -> str:
        if model not in ("launch", "train"):
            raise ValueError("model must be launch (default) or train")
        if not features:
            raise ValueError("A release needs at least one feature")
        known = {f["feature_id"]: f for f in self.feature_summaries(product_id)}
        for fid in features:
            f = known.get(fid)
            if not f:
                raise ValueError(f"{fid} is not a feature of {product_id}")
            if f["status"] != "in_progress":
                raise ValueError(f"{fid} is {f['status']}; only features still in progress can join a release")
            if (self._state(fid).get("approvals") or {}).get("g6"):
                raise ValueError(f"{fid} has already passed G6")
            other = self.release_for(fid, active=True)
            if other:
                raise ValueError(f"{fid} is already in {other['id']}")
        rid = self._new_id(product_id, "release", title)
        inp = {"release_id": rid, "product_id": product_id, "title": title, "model": model,
               "features": list(dict.fromkeys(features)), "target_date": target_date, "initiative": initiative,
               "created_by": created_by}
        self.bus.publish({"type": "feature.created", "feature_id": rid, "product_id": product_id})
        (self._submit if background else self._execute)(rid, inp)
        return rid
