"""Costs, the resource inventory and looking back over a product's changes."""

from __future__ import annotations

import logging



from .base import kind_of

log = logging.getLogger(__name__)


class CostsMixin:
    """FeatureEngine's costs methods (valxel_sdlc.engine)."""

    def inventory(self, product_id: str) -> dict:
        """What the product runs in each environment, read live (ops/inventory.py)."""
        from ..ops.inventory import collect

        known: dict = {"components": {}, "resources": {}, "store_apps": {}}
        for row in self._rows(product_id, "feature"):
            s = self._state(row["feature_id"])
            for r in s.get("resources") or []:
                known["resources"].setdefault(r["name"], {"type": r.get("type", ""), "sizes": r.get("sizes") or {},
                                                          "feature_id": row["feature_id"]})
            for key, d in (s.get("deployments") or {}).items():
                if d.get("kind") == "resource":
                    continue
                if d.get("kind") == "store":
                    if d.get("env") == "production" or key not in known["store_apps"]:
                        known["store_apps"][key] = {"env": d.get("env", ""), "version": str(d.get("release") or d.get("sha", ""))[:40],
                                                    "feature_id": row["feature_id"]}
                    continue
                known["components"][d.get("repo") or key] = {
                    "component": d.get("component") or key, "project": d.get("project"), "namespace": d.get("namespace"),
                    "feature_id": row["feature_id"]}  # features in order: the latest release wins
        snap = self.services.snapshot(product_id)
        costs = self.services.cost_ledger.query(product_id=product_id, stage="run") + \
            self.services.cost_ledger.query(product_id=product_id, stage="build")
        return collect(self.services.openchoreo, snap, known, costs=costs, aws=self.services.aws_tags)

    def snapshot_inventories(self, day: str | None = None) -> int:
        """Keep today's counts for every product (the watcher, once a day)."""
        from ..ops.inventory import snapshot_row

        n = 0
        for pid in self.services.settings.registry:
            try:
                if self.services.snapshot(pid).product.runtime is None:
                    continue  # a shared lane runs nothing
                row = snapshot_row(self.inventory(pid))
                self.services.inventory_snapshots.put({**row, "day": day or row["day"]})
                n += 1
            except Exception:
                continue
        return n

    def lookback(self, product_id: str, months: int = 6, now=None) -> dict:
        """Incidents and changes per month (ops/lookback.py)."""
        from ..ops.lookback import months_back, report

        incidents = [{**self._state(r["feature_id"]), "incident_id": r["feature_id"]}
                     for r in self._rows(product_id, "incident")]
        log = self.services.changes
        return {"product_id": product_id,
                **report(incidents, log.list(product_id), log.links(product_id), months_back(months, now))}

    # --- cost: released features against what G4 approved -----------------------------------
    def review_costs(self, at=None) -> list[dict]:
        """Review each released feature that has reached a review day without a review for it:
        AWS spend against its G4 plan, production compute the week after release against the week
        before. Findings go into the incident flow as cost signals. Returns the reviews made."""
        from ..ops import cost

        at = at or cost.now()
        made = []
        for row in self.index.all():
            if row.get("kind", "feature") != "feature":
                continue
            try:
                s = self._state(row["feature_id"])
                released = cost.released_at(s) if s.get("status") == "done" else None
                if not released or not s.get("cost_plan"):
                    continue
                elapsed = (at - released).total_seconds() / 86400
                due = [d for d in self.cost_review_days if elapsed >= d]
                done = {r.get("review_day") for r in self.services.cost_reviews.for_feature(row["feature_id"])}
                todo = [d for d in due if d not in done]
                if not todo:
                    continue
                rv = self.review_cost(s, at=at, released=released)
                rv["review_day"] = max(todo)  # catching up on several days makes one review
                for d in todo[:-1]:
                    self.services.cost_reviews.add({**rv, "review_day": d, "skipped": True})
                self.services.cost_reviews.add(rv)
                made.append(rv)
            except Exception as exc:
                self.errors[f"cost:{row['feature_id']}"] = f"{type(exc).__name__}: {exc}"
        return made

    def review_cost(self, s: dict, *, at, released) -> dict:
        from datetime import timedelta

        from ..ops import cost

        src = self.services.costs
        plan = s["cost_plan"]
        window = min(7.0, max(1.0, (at - released).total_seconds() / 86400))
        start = at - timedelta(days=window)
        aws_actual = {}
        for env, account in sorted((plan.get("accounts") or {}).items()):
            if env in (plan.get("aws_monthly_usd") or {}):
                aws_actual[env] = src.aws_costs(product_id=s["product_id"], feature_id=s["feature_id"],
                                                account=account, start=start, end=at)
        before, after, repo_of = {}, {}, {}
        groups: dict[tuple, list[str]] = {}
        for repo, dep in sorted((s.get("deployments") or {}).items()):
            if dep.get("kind") in ("resource", "store") or not dep.get("component"):
                continue
            if dep.get("env") != (list(dep.get("pipeline") or ["production"]))[-1]:
                continue  # production only
            repo_of[dep["component"]] = repo
            groups.setdefault((dep.get("namespace") or "default", dep.get("project") or "", dep["env"]), []).append(dep["component"])
        for (ns, project, env), comps in groups.items():
            after |= src.component_costs(namespace=ns, project=project, env=env, components=comps,
                                         start=max(start, released), end=at)
            before |= src.component_costs(namespace=ns, project=project, env=env, components=comps,
                                          start=released - timedelta(days=window), end=released)
        rv = cost.review(plan, feature_id=s["feature_id"], product_id=s["product_id"], released=released, at=at,
                         aws_actual=aws_actual, window_days=window, compute_before=before, compute_after=after,
                         tolerance=self.cost_tolerance)
        sigs = cost.signals(rv, title=s.get("title", ""), repo_of=lambda c: repo_of.get(c, c))
        rv["incidents"] = self.ingest(sigs) if sigs else []
        return rv

    def _run_cost(self, feature_id: str, s: dict) -> dict | None:
        """For the feature page: production's approved monthly AWS cost and the latest review."""
        plan = s.get("cost_plan") or {}
        if not plan.get("resources"):
            return None
        reviews = [r for r in self.services.cost_reviews.for_feature(feature_id) if not r.get("skipped")]
        last = reviews[-1] if reviews else None
        return {"approved_monthly_usd": plan.get("aws_monthly_usd") or {},
                "projected_monthly_usd": ({e: v["projected_monthly_usd"] for e, v in last["aws"].items()} if last else None),
                "reviewed_at": last["at"] if last else None,
                "findings": [f["text"] for f in last["findings"]] if last else [],
                "incidents": last.get("incidents", []) if last else []}

    def collect_costs(self, at=None, product_ids=None) -> int:
        """Bring the cost ledger up to date (valxel_sdlc.finops): every product's agent calls, CI,
        builds, environments, production compute and AWS, then the shared platform's share.
        Returns the records written."""
        from ..finops import collect as fc
        from ..finops.views import month_bounds
        from ..ops import cost

        at = at or cost.now()
        r = fc.rates()
        ledger = self.services.cost_ledger
        since = at.timestamp() - fc.LOOKBACK_DAYS * 86400
        usage = [u for u in self.services.ledger.records() if fc.utc(u.ts).timestamp() >= since]
        written, compute = 0, {}
        reg = self.services.settings.registry
        for pid in (product_ids or list(reg)):
            try:
                written += self._collect_product(pid, at, r, [u for u in usage if u.product_id == pid])
            except Exception as exc:
                self.errors[f"cost:{pid}"] = f"{type(exc).__name__}: {exc}"
                continue
            _, start, end = month_bounds(None, at)
            compute[pid] = sum(x.usd for x in ledger.query(product_id=pid, stage="run", start=start, end=end)
                               if x.category == "compute")
        if not product_ids:
            written += ledger.upsert(fc.collect_platform(compute, at.date(), at, r))
        return written

    def _collect_product(self, pid: str, at, r: dict, usage: list) -> int:
        from ..finops import collect as fc
        from ..ops import cost

        ledger = self.services.cost_ledger
        recs = fc.collect_llm(usage, kind_of, lambda t: None)
        snap = self.services.snapshot(pid)
        windows: dict[tuple, list] = {}
        releases = {}
        for row in self._rows(pid, "feature"):
            fid = row["feature_id"]
            s = self._state(fid)
            if not s:
                continue
            releases[fid] = cost.released_at(s) if s.get("status") == "done" else None
            end = fc.ended_at(s, at)
            if (at - end).days > fc.LOOKBACK_DAYS:
                continue  # settled long ago
            recs += fc.collect_ci(self.services.github, s, r)
            recs += fc.collect_builds(self.services.openchoreo, s, r)
            recs += fc.collect_people(s, r)
            for env, (a, b) in fc.env_windows(s, at).items():
                for dep in (s.get("deployments") or {}).values():
                    if dep.get("kind") in ("resource", "store") or not dep.get("component"):
                        continue
                    windows.setdefault((dep.get("project") or "", env, dep["component"]), []).append((fid, a, b))
        targets = set()
        runtimes = [snap.product.runtime or {}] + [lane.runtime or {} for lane in snap.lane_of_repo().values()]
        for rt in runtimes:
            if not rt.get("openchoreo_project"):
                continue
            pipeline = list(rt.get("pipeline") or ["dev", "staging", "production"])
            for env in pipeline:
                targets.add((rt.get("namespace") or "default", rt["openchoreo_project"], env, env == pipeline[-1]))
        recs += fc.collect_runtime(self.services.costs, pid, sorted(targets), windows, at, r)
        aws = (snap.product.runtime or {}).get("aws") or {}
        recs += fc.collect_aws(self.services.costs, pid, dict(aws.get("accounts") or {}), releases, at, r)
        return ledger.upsert(recs)

    def feature_cost(self, feature_id: str) -> dict:
        """The feature's cost card: build cost by category, its own resources' run cost, the plan G4
        approved, and the reviews since release."""
        from ..finops import collect as fc
        from ..finops.views import feature_card

        s = self._state(feature_id) if kind_of(feature_id) == "feature" else {}
        if not s:
            raise KeyError(feature_id)
        # Agent spend is always current: it is in the tool's own ledger.
        self.services.cost_ledger.upsert(fc.collect_llm(self.services.ledger.records(feature_id), kind_of,
                                                        lambda t: None))
        return feature_card(self.services.cost_ledger, s,
                            reviews=self.services.cost_reviews.for_feature(feature_id))

    def product_cost(self, product_id: str, month: str | None = None) -> dict:
        """The product's month: run cost by category and environment, build cost by feature."""
        from ..finops.views import product_rollup

        features = []
        for f in self.feature_summaries(product_id):
            plan = self._state(f["feature_id"]).get("cost_plan") or {}
            features.append({**f, "approved_monthly_usd": plan.get("aws_monthly_usd") or {}})
        return product_rollup(self.services.cost_ledger, product_id, features, month=month)

    def focus_export(self, product_id: str, month: str | None = None) -> str:
        from ..finops.ledger import focus_csv
        from ..finops.views import month_bounds
        from ..ops.cost import now

        _, start, end = month_bounds(month, now())
        return focus_csv(self.services.cost_ledger.query(product_id=product_id, start=start, end=end))
