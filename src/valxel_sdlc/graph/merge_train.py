"""The merge train, waiting on CI, security review and fixing failures before production."""

from __future__ import annotations

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..state import FeatureState
from .. import store
from ..waits import LABELS, TIMEOUT_S
from .phase import event, gates_status, now
from .infra import oc_target
from .routing import ESCALATION_DECISIONS, SECURITY_DECISIONS, _record, after_ci, bump_for, normalize


def make_merge_train(services):
    def merge_train(state: FeatureState) -> dict:
        cs = dict(state.get("change_set") or {})
        teardown_events = []
        env = cs.get("env") or {}
        if env.get("name") and not env.get("deleted"):  # the change set worked together: its environment goes
            from .. import changeset_env

            problems = changeset_env.teardown(services, env)
            cs["env"] = {**env, "deleted": True}
            teardown_events.append(event("merge_train", "change_set_env_deleted", env["name"], problems=problems))
        risk = state.get("risk_tier", "low")
        lanes = services.feature_snapshot(state).lane_of_repo()
        g5 = [g for g in state.get("gate_decisions", []) if g["gate"] == "g5" and g["decision"] in ("approve", "handback")]
        actor = g5[-1]["approver"] if (risk != "low" and g5) else "agent:merge-train"
        merged, published, runs = {}, list(cs.get("published") or []), dict(cs.get("runs") or {})
        packages = dict(cs.get("packages") or {})
        passed = ", ".join(sorted({g["gate"].upper() for g in state.get("gate_decisions", [])
                                   if g["decision"] in ("approve", "handback")})) or "gates"
        failed_runs = {r for r, v in runs.items() if v.get("state") == "failure"}
        roles = {t["repo"]: t.get("role") for t in state.get("tasks") or []}
        for repo in cs.get("merge_order", []):  # dependency order: libraries, providers, consumers
            sha = ""
            for key, pr in sorted(state.get("pull_requests", {}).items()):
                if pr["repo"] != repo:
                    continue
                if pr.get("state") != "merged":  # idempotent: a retry skips what is merged
                    # Every gate this change needs is approved: say so on the commit the gates saw,
                    # and merge exactly that commit.
                    gates_status(services, [pr], "success", f"{state['feature_id']}: {passed} approved")
                    res = services.github.merge(repo=repo, number=pr["number"], actor=actor,
                                                sha=pr.get("head_sha") or "")
                    pr = {**pr, "state": res["state"], "merged_by": actor, "merge_sha": res.get("merge_sha")}
                    merged[key] = pr
                sha = pr.get("merge_sha") or sha
            lane = lanes.get(repo)
            if roles.get(repo) == "acceptance":
                # Not deployed: the pipeline runs these tests against each environment (env waits).
                cs["acceptance"] = {"repo": repo, "sha": sha}
                continue
            if roles.get(repo) in store.STORE_ROLES:
                # Released through the stores by its own workflow at each stage (valxel_sdlc.store).
                cs.setdefault("store", {})[repo] = {"repo": repo, "sha": sha}
                continue
            if lane is not None and lane.kind == "shared":
                # No runtime of its own: publish a release candidate that consumers build against.
                if repo not in published:  # tags the merge commit; the repo's workflow publishes it
                    packages[repo] = services.packages.publish(repo=repo, version=None, channel="rc", actor=actor,
                                                               sha=sha, bump=bump_for(state, repo))
                    published.append(repo)
            elif repo not in runs or repo in failed_runs:  # build the merge commit in OpenChoreo
                t = oc_target(lane, repo)
                run = services.openchoreo.run_workflow(component=t["component"], ref=sha or "main", kind="main",
                                                       project=t["project"], namespace=t["namespace"])
                runs[repo] = {"repo": repo, "sha": sha, **t, "pipeline": list(t["pipeline"]),
                              "run_id": run["run_id"], "url": run.get("url", "")}
        cs |= {"merged": True, "published": published, "packages": packages, "runs": runs}
        return {"pull_requests": merged, "change_set": cs,
                "events": teardown_events + [event("merge_train", "merged", f"{len(merged)} PRs by {actor}",
                                 order=cs.get("merge_order", []), published=published,
                                 versions={r: p["tag"] for r, p in packages.items()})]}

    return merge_train


def external(state: FeatureState, wait: str, targets: list[dict], quality: dict | None = None) -> dict:
    packet = {"type": "external", "wait": wait, "label": LABELS[wait], "feature_id": state["feature_id"],
              "product_id": state["product_id"], "title": state["title"], "targets": targets, "since": now(),
              "timeout_s": TIMEOUT_S[wait]}
    if quality:
        packet["quality"] = quality
    return packet


def quality_packet(services, state: FeatureState, wait: str, targets: list[dict]) -> dict | None:
    """The product's suites for this stage, travelling with the wait so the checker needs no state."""
    from ..quality import POLICY_STAGE, WAIT_STAGE

    stage = WAIT_STAGE[wait]
    policy = services.feature_snapshot(state).product.quality or {}
    mine = {k: v for k, v in policy.items() if v.get("stage") == POLICY_STAGE.get(stage, stage)}
    if not mine or not targets:
        return None
    return {"stage": stage, "policy": mine, "targets": targets, "waivers": list(state.get("waivers") or [])}


def _quality_update(res: dict) -> dict:
    q = res.get("quality")
    return {"quality": {q["stage"]: {**q, "at": res.get("at", now())}}} if q else {}


def security_review(services, state: FeatureState, wait: str, res: dict, *, ok: str, retry: str,
                    retry_update: dict | None = None, extra: dict | None = None) -> Command:
    """Blocking findings go to the lane's security approvers (approvers.security, else G5's).
    retry: fix (before merge, agents get the findings) or scan again; accept_risk: a waiver with a
    reason and an expiry; abort: stop the feature."""
    q = res["quality"]
    packet = {"type": "escalation", "phase": "security", "wait": wait, "stage": q["stage"],
              "feature_id": state["feature_id"], "product_id": state["product_id"], "title": state["title"],
              "reason": res.get("reason", ""), "blocking": q["blocking"],
              "findings": [f"{f['severity'].upper()} {f['title']} ({f['repo']}, {f['tool']})" for f in q["blocking"]],
              "max_waiver_days": services.settings.quality.waiver_max_days if services.settings.quality else 90,
              "cost_usd": round(float(state.get("cost_usd", 0.0)), 4), "budget_usd": state.get("token_budget_usd"),
              "allowed": list(SECURITY_DECISIONS)}
    d = normalize(interrupt(packet), SECURITY_DECISIONS)
    update = {"gate_decisions": [_record(f"escalation:security:{q['stage']}", "security", d)],
              "delivery": {wait: res}, **_quality_update(res), **(extra or {})}
    if d["decision"] == "abort":
        return Command(goto=END, update=update | {"status": "aborted", "events": [
            event("security", "aborted", d["feedback"], approver=d["approver"])]})
    if d["decision"] == "accept_risk":
        from ..quality import waiver

        max_days = packet["max_waiver_days"]
        waivers = [waiver(f, approver=d["approver"], reason=d["feedback"], days=d.get("expires_days") or max_days,
                          max_days=max_days) for f in q["blocking"]]
        accepted = {w["fingerprint"] for w in waivers}
        q = {**q, "status": "passed", "blocking": [], "suites": [
            {**r, "waived": sorted(set(r.get("waived") or []) | {f["fingerprint"] for f in r.get("findings") or []
                                                               if f["fingerprint"] in accepted})}
            for r in q["suites"]]}
        update |= {"quality": {q["stage"]: {**q, "at": now()}}, "delivery": {wait: {**res, "status": "accepted"}}}
        return Command(goto=ok, update=update | {"waivers": waivers, "events": [
            event("security", "accepted_risk", d["feedback"], approver=d["approver"], findings=len(waivers),
                  expires=waivers[0]["expires"] if waivers else "")]})
    return Command(goto=retry, update=update | (retry_update or {}) | {"events": [
        event("security", "retry", d["feedback"] or res.get("reason", ""), approver=d["approver"])]})


def failing_repos(state: FeatureState, res: dict) -> list[str]:
    """The repos of this change set whose code a failure points at: failed builds, deployments and
    suites, blocking findings, and for a failed acceptance criterion the repos whose tasks cover it.
    Only those the feature's tasks change can be fixed by its agents."""
    tasks = state.get("tasks") or []
    ours = {t["repo"] for t in tasks}
    repos = {r.get("repo") for r in res.get("results") or [] if r.get("state") not in ("success", None)}
    q = res.get("quality") or {}
    for s in q.get("suites") or []:
        if s.get("state") == "failed":
            repos.add(s.get("repo"))
            failed = [ac for ac, st in ((s.get("details") or {}).get("criteria") or {}).items() if st == "failed"]
            repos |= {t["repo"] for t in tasks if set(failed) & set(t.get("acs") or [])}
    repos |= {f.get("repo") for f in q.get("blocking") or []}
    return sorted(r for r in repos if r and r in ours)


def failure_lines(res: dict) -> list[str]:
    q = res.get("quality") or {}
    lines = list(q.get("test_failures") or [])
    lines += [f"{f['severity']} {f['title']} in {f['repo']} ({f.get('url', '')})" for f in q.get("blocking") or []]
    lines += [f"{r.get('repo') or r.get('component')}: {r.get('message') or r.get('state')}"
              for r in res.get("results") or [] if r.get("state") not in ("success", None)]
    return list(dict.fromkeys(lines)) or [res.get("reason", "it failed")]


PRE_PRODUCTION = ("build", "change_set", "dev", "staging")


def fix_or_ask(services, state: FeatureState, wait: str, res: dict, people, extra: dict | None = None) -> Command:
    """A failure before production goes back to the agents first, with what failed: they fix the
    repos it points at, and the change goes through CI, the merge, the build and the environments
    again. People decide only when the tries run out (the product's max_loops) or nothing the
    feature changed is to blame (a platform outage, a managed service), through `people`."""
    failing = failing_repos(state, res)
    tries = int(state.get("fix_retries") or 0)
    limit = services.feature_snapshot(state).product.max_loops
    if wait not in PRE_PRODUCTION or not failing or tries >= limit:
        return people()
    n = tries + 1
    lines = failure_lines(res)
    what = {"build": "The OpenChoreo build failed", "change_set": "The change set's environment failed",
            "dev": "Dev failed", "staging": "Staging failed"}[wait]
    update = {**(extra or {}), "delivery": {wait: res}, **_quality_update(res), "fix_retries": n,
              "rerun_repos": failing, "ci_retries": 0,
              "feedback": {"build": f"{what} {'after the change merged' if (state.get('change_set') or {}).get('merged') else 'with the pull requests together, before merging'}. Fix these failures:\n"
                                    + "\n".join(f"- {x}" for x in lines)},
              "events": [event(f"{wait}_wait", "auto_fix", f"{what} in {', '.join(failing)}: agents fix it "
                               f"(try {n} of {limit})", findings=lines)]}
    if (state.get("change_set") or {}).get("merged"):
        update["fix_branch"] = f"feature/{state['feature_id']}-fix-{n}"  # the merged branch can't take new commits
    return Command(goto="build", update=update)


def ask_release_managers(state: FeatureState, wait: str, res: dict) -> dict:
    """A failed CI-to-production step goes to people, like a stuck phase."""
    packet = {"type": "escalation", "phase": "delivery", "wait": wait, "feature_id": state["feature_id"],
              "product_id": state["product_id"], "title": state["title"], "reason": res.get("reason", ""),
              "findings": [f"{r.get('repo') or r.get('component')}: {r.get('message') or r.get('state')}"
                           for r in res.get("results", []) if r.get("state") != "success"],
              "results": res.get("results", []), "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
              "budget_usd": state.get("token_budget_usd"), "allowed": list(ESCALATION_DECISIONS)}
    return normalize(interrupt(packet), ESCALATION_DECISIONS)


def _people_said(state, wait: str, res: dict, *, ok: str, retry: str, extra: dict | None = None) -> Command:
    d = ask_release_managers(state, wait, res)
    update = {"gate_decisions": [_record(f"escalation:delivery:{wait}", "delivery", d)],
              "delivery": {wait: res}, **(extra or {})}
    if d["decision"] == "abort":
        return Command(goto=END, update=update | {"status": "aborted",
                                                  "events": [event(f"{wait}_wait", "aborted", d["feedback"],
                                                                   approver=d["approver"])]})
    kind = "retry" if d["decision"] == "retry" else "resolved"
    return Command(goto=retry if kind == "retry" else ok,
                   update=update | {"events": [event(f"{wait}_wait", kind, d["feedback"] or res.get("reason", ""),
                                                     approver=d["approver"])]})


def report_to_github(services, state: FeatureState, wait: str, res: dict) -> None:
    """Post OpenChoreo's build and deploy results on the merge commit, so GitHub shows how far each
    commit got (contexts openchoreo/build, openchoreo/dev, ...). Best effort: never blocks delivery."""
    runs = (state.get("change_set") or {}).get("runs") or {}
    for r in res.get("results", []):
        run = runs.get(r.get("repo"), {})
        if not run.get("sha"):
            continue
        ok = r.get("state") == "success"
        try:
            services.github.set_status(
                repo=r["repo"], sha=run["sha"], context=f"openchoreo/{wait}",
                state="success" if ok else "failure",
                description=(f"{LABELS[wait]}: done" if ok else (r.get("message") or f"{LABELS[wait]} failed")),
                url=r.get("url") or run.get("url", ""))
        except Exception:
            pass


def make_ci_wait(services):
    def ci_wait(state: FeatureState) -> Command:
        prs = [pr for pr in (state.get("pull_requests") or {}).values()
               if pr.get("state") != "merged" and pr.get("head_sha")]
        nxt = after_green_ci(services, state)
        if not prs:
            return Command(goto=nxt)
        perf = state.get("performance") or {}
        targets = [{"repo": p["repo"], "number": p["number"], "sha": p["head_sha"], "base": p.get("base") or "main",
                    "url": p["url"], "perf": (perf.get(p["repo"]) or {}).get("level", "none")} for p in prs]
        res = interrupt(external(state, "ci", targets, quality_packet(services, state, "ci", targets)))
        if res["status"] == "success":
            return Command(goto=nxt, update={"delivery": {"ci": res}, **_quality_update(res), "ci_retries": 0,
                                             "events": [event("ci_wait", "passed", f"CI green on {len(prs)} PRs")]})
        if res["status"] == "security":
            fix = "Fix these security findings:\n" + "\n".join(
                f"- {f['severity']} {f['title']} in {f['repo']} ({f['url']})" for f in res["quality"]["blocking"])
            # The agents fix blocking findings first; security approvers decide (fix, accept the
            # risk, or stop) only when the tries run out.
            tries, limit = int(state.get("ci_retries") or 0), services.feature_snapshot(state).product.max_loops
            failing = failing_repos(state, res)
            if tries < limit and failing:
                return Command(goto="build", update={
                    "delivery": {"ci": res}, **_quality_update(res), "ci_retries": tries + 1, "rerun_repos": failing,
                    "feedback": {"build": fix},
                    "events": [event("ci_wait", "auto_fix", f"Security findings in {', '.join(failing)}: agents fix "
                                     f"them (try {tries + 1} of {limit})", findings=failure_lines(res))]})
            return security_review(services, state, "ci", res, ok=nxt, retry="build",
                                   retry_update={"feedback": {"build": fix}})
        findings = (list((res.get("quality") or {}).get("test_failures") or []) or
                    [f"{r['repo']}#{r['number']}: {r.get('message') or 'checks failed'}"
                     for r in res["results"] if r["state"] == "failure"])
        # Red CI goes back to the agents first, with what failed, and only the failing repos' tasks
        # are redone. People see it only when the retries run out (the product's max_loops).
        tries = int(state.get("ci_retries") or 0)
        limit = services.feature_snapshot(state).product.max_loops
        failing = sorted({r["repo"] for r in res.get("results") or [] if r.get("state") == "failure"})
        if tries < limit and failing:
            fix = "CI failed on the pull requests. Fix these failures:\n" + "\n".join(f"- {f}" for f in findings)
            return Command(goto="build", update={
                "delivery": {"ci": res}, **_quality_update(res), "ci_retries": tries + 1, "rerun_repos": failing,
                "feedback": {"build": fix},
                "events": [event("ci_wait", "retry", f"CI red in {', '.join(failing)}: agents fix it "
                                 f"(try {tries + 1} of {limit})", findings=findings)]})
        # The retries ran out: people decide (let agents try again, vouch for it, or stop).
        return Command(goto="escalate_build", update={
            "delivery": {"ci": res}, **_quality_update(res), "phase_status": {"build": "escalated"},
            "escalations": {"build": {"phase": "build", "reason": res["reason"], "findings": findings}},
            "events": [event("ci_wait", "failed", res["reason"])]})

    return ci_wait, ("cs_env", "g5", "merge_train", "escalate_build", "build", END)


def after_green_ci(services, state: FeatureState) -> str:
    """Green CI goes to the change set's own environment first, when the product runs one."""
    from .. import changeset_env

    snap = services.feature_snapshot(state)
    cfg = changeset_env.config(snap.product.runtime)
    if snap.product.kind != "product" or not snap.product.runtime or not cfg["enabled"]:
        return after_ci(state)
    if not changeset_env.components(state, snap.lane_of_repo()):
        return after_ci(state)
    return "cs_env"
