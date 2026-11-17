"""Change-set environments, builds and deployments to dev and staging."""

from __future__ import annotations

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..integrations.openchoreo import PIPELINE
from ..state import FeatureState
from ..waits import STORE_TIMEOUT_S
from .phase import event, now
from .infra import _apply_capacity, _ensure_alert_rules, _staging_targets, bind_resource, env_for, oc_target
from .merge_train import _people_said, _quality_update, external, fix_or_ask, quality_packet, report_to_github, security_review
from .rollout import acceptance_targets, datasets_staging, instrumentation_staging
from .routing import after_ci, release_store_app


def make_cs_env(services):
    """Create the change set's environment (once) and build each runtime repo's pull request head."""

    def cs_env(state: FeatureState) -> Command:
        from .. import changeset_env

        snap = services.feature_snapshot(state)
        cfg = changeset_env.config(snap.product.runtime)
        targets = changeset_env.components(state, snap.lane_of_repo())
        cs = dict(state.get("change_set") or {})
        env = dict(cs.get("env") or {})
        name = env.get("name") or changeset_env.name_for(state["feature_id"])
        namespaces = sorted({t["namespace"] for t in targets}, key=str)
        try:
            if not env.get("name"):
                if changeset_env.open_count(services, namespaces) >= cfg["max_open"]:
                    raise RuntimeError(f"{cfg['max_open']} change-set environments are already open "
                                       "(runtime.change_sets.max_open)")
                for ns in namespaces:
                    services.openchoreo.create_environment(
                        name=name, like=targets[0]["like"], project=targets[0]["project"], namespace=ns,
                        labels={"valxel.io/feature": state["feature_id"]})
            runs = {}
            for t in targets:
                run = services.openchoreo.run_workflow(component=t["component"], ref=t["sha"], kind="change_set",
                                                       project=t["project"], namespace=t["namespace"])
                runs[t["repo"]] = {**t, "run_id": run["run_id"], "url": run.get("url", ""), "state": "running"}
        except Exception as exc:
            reason = f"Couldn't set up the change set's environment: {type(exc).__name__}: {exc}"
            if not cfg["required"]:  # optional: say so and carry on as before
                return Command(goto=after_ci(state), update={"events": [event("cs_env", "skipped", reason)]})
            res = {"status": "failure", "wait": "change_set", "results": [], "reason": reason, "at": now()}
            return _people_said(state, "change_set", res, ok=after_ci(state), retry="cs_env")
        cs["env"] = {**env, "name": name, "namespaces": namespaces, "runs": runs, "deployed": env.get("deployed") or {},
                     "created": env.get("created") or now(), "deleted": False}
        return Command(goto="cs_build_wait", update={"change_set": cs, "events": [
            event("cs_env", "building", f"{name}: {len(runs)} pull request heads", env=name)]})

    return cs_env, ("cs_build_wait", "g5", "merge_train", "cs_env", END)


def make_cs_build_wait(services):
    def cs_build_wait(state: FeatureState) -> Command:
        cs = dict(state.get("change_set") or {})
        env = dict(cs.get("env") or {})
        runs = env.get("runs") or {}
        todo = [r for r in runs.values() if r.get("state") != "success"]
        if not todo:
            return Command(goto="cs_deploy")
        res = interrupt(external(state, "build", todo))
        env["runs"] = {**runs, **{r["repo"]: {**runs[r["repo"]], "state": r["state"]} for r in res["results"]}}
        cs["env"] = env
        if res["status"] == "success":
            return Command(goto="cs_deploy", update={"change_set": cs, "events": [
                event("cs_build_wait", "passed", f"{len(todo)} pull request heads built for {env['name']}")]})
        return fix_or_ask(services, state, "build", res, lambda: _people_said(
            state, "build", res, ok="cs_deploy", retry="cs_env", extra={"change_set": cs}), extra={"change_set": cs})

    return cs_build_wait, ("cs_deploy", "cs_env", "cs_build_wait", "build", END)


def make_cs_deploy(services):
    """Bind the feature's managed services and each build to the change set's environment."""

    def cs_deploy(state: FeatureState) -> Command:
        cs = dict(state.get("change_set") or {})
        env = dict(cs.get("env") or {})
        name, deployed, failures = env["name"], dict(env.get("deployed") or {}), []
        for spec in state.get("resources") or []:
            spec = spec.get("spec") or spec
            try:
                services.openchoreo.apply_resource(name=spec["name"], type=spec["type"], parameters=spec["parameters"],
                                                   project=spec["project"], namespace=spec.get("namespace"))
                # The smallest size, never retained: test data goes with the environment.
                services.openchoreo.bind_resource(name=spec["name"], env=name, size="small", actor="agent:change-set",
                                                  actor_type="agent", project=spec["project"],
                                                  namespace=spec.get("namespace"), pipeline=(name, "production"))
                deployed[f"resource:{spec['name']}"] = {"repo": f"resource:{spec['name']}", "kind": "resource",
                                                        "component": spec["name"], "project": spec["project"],
                                                        "namespace": spec.get("namespace"), "env": name}
            except Exception as exc:
                failures.append({"repo": f"resource:{spec['name']}", "component": spec["name"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        for repo, run in (env.get("runs") or {}).items():
            try:
                rec = services.openchoreo.bind_latest(component=run["component"], env=name, actor="agent:change-set",
                                                      project=run["project"], namespace=run["namespace"])
                deployed[repo] = {"repo": repo, "component": run["component"], "project": run["project"],
                                  "namespace": run["namespace"], "env": name, "sha": run["sha"],
                                  "role": run.get("role", ""), "release": rec.get("release", "")}
            except Exception as exc:
                failures.append({"repo": repo, "component": run["component"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        env["deployed"] = deployed
        cs["env"] = env
        if failures:
            res = {"status": "failure", "wait": "change_set", "results": failures, "at": now(),
                   "reason": f"Couldn't deploy to {name}: " + "; ".join(f["message"] for f in failures)}
            return _people_said(state, "change_set", res, ok=after_ci(state), retry="cs_deploy",
                                extra={"change_set": cs})
        return Command(goto="cs_wait", update={"change_set": cs, "events": [
            event("cs_deploy", "deployed", f"{name}: {', '.join(sorted(deployed))}", env=name)]})

    return cs_deploy, ("cs_wait", "g5", "merge_train", "cs_deploy", END)


def make_cs_wait(services):
    """Wait until the change set runs in its environment, then run the dev-stage suites and the
    feature's acceptance tests there. Passed: on to G5 or the merge; failed: back to the agents."""

    def cs_wait(state: FeatureState) -> Command:
        from .. import changeset_env

        env = (state.get("change_set") or {}).get("env") or {}
        deployed = list((env.get("deployed") or {}).values())
        targets = [{"repo": d["repo"], "component": d["component"], "project": d.get("project"),
                    "namespace": d.get("namespace"), "env": d["env"],
                    **({"kind": "resource"} if d.get("kind") == "resource" else {})} for d in deployed]
        nxt = after_ci(state)
        if not targets:
            return Command(goto=nxt)
        perf = state.get("performance") or {}
        qtargets = [{**t, "sha": d.get("sha", ""), "role": d.get("role", ""), "perf": (perf.get(t["repo"]) or {}).get("level", "none")}
                    for t, d in zip(targets, deployed) if t.get("kind") != "resource"]
        acc = changeset_env.acceptance_head(state)
        if acc:
            qtargets += acceptance_targets(services, {**state, "change_set": {**(state.get("change_set") or {}),
                                                                              "acceptance": acc}},
                                           "change_set", [t for t in targets if t.get("kind") != "resource"])
        res = interrupt(external(state, "change_set", targets, quality_packet(services, state, "change_set", qtargets)))
        if res["status"] == "security":
            return fix_or_ask(services, state, "change_set", res, lambda: security_review(
                services, state, "change_set", res, ok=nxt, retry="cs_wait"))
        if res["status"] == "success":
            return Command(goto=nxt, update={"delivery": {"change_set": res}, **_quality_update(res),
                                             "events": [event("cs_wait", "passed",
                                                              f"the change set works together in {env['name']}")]})
        return fix_or_ask(services, state, "change_set", res, lambda: _people_said(
            state, "change_set", res, ok=nxt, retry="cs_deploy"))

    return cs_wait, ("g5", "merge_train", "cs_wait", "cs_deploy", "build", END)


def make_build_wait(services):
    def build_wait(state: FeatureState) -> Command:
        runs = (state.get("change_set") or {}).get("runs") or {}
        todo = [r for r in runs.values() if r.get("state") != "success"]
        if not todo:
            return Command(goto="deploy_dev")
        res = interrupt(external(state, "build", todo, quality_packet(services, state, "build", todo)))
        report_to_github(services, state, "build", res)
        cs = dict(state.get("change_set") or {})
        cs["runs"] = {**runs, **{r["repo"]: {**runs[r["repo"]], "state": r["state"]} for r in res["results"]}}
        if res["status"] == "security":
            return fix_or_ask(services, state, "build", res, lambda: security_review(
                services, state, "build", res, ok="deploy_dev", retry="build_wait", extra={"change_set": cs}))
        if res["status"] == "success":
            return Command(goto="deploy_dev", update={"delivery": {"build": res}, "change_set": cs, **_quality_update(res),
                                                      "events": [event("build_wait", "passed",
                                                                       f"{len(todo)} OpenChoreo builds succeeded")]})
        return fix_or_ask(services, state, "build", res, lambda: _people_said(
            state, "build", res, ok="deploy_dev", retry="merge_train", extra={"change_set": cs, **_quality_update(res)}))

    return build_wait, ("deploy_dev", "merge_train", "build_wait", "build", END)


def make_deploy(stage: str, services, nxt: str):
    """Agents deploy to dev and staging only (VX-3); each component in its own product's runtime."""

    def deploy(state: FeatureState) -> Command:
        snap = services.feature_snapshot(state)
        lanes = snap.lane_of_repo()
        deps, failures, notes = {}, [], []
        # Managed services first, so the components that use them find them (VX-19).
        for spec in state.get("resources") or []:
            try:
                deps[f"resource:{spec['name']}"] = bind_resource(services, state, spec, stage,
                                                                 actor=f"agent:{stage}", actor_type="agent")
            except Exception as exc:
                failures.append({"repo": f"resource:{spec['name']}", "component": spec["name"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        for repo, run in ((state.get("change_set") or {}).get("runs") or {}).items():
            t = oc_target(lanes.get(repo), repo)
            env = env_for(stage, t["pipeline"])
            if env is None:
                continue
            # The platform's default alert rules, before the component runs anywhere (alert-rules.yaml).
            lane_repos = getattr(lanes.get(repo), "repos", None) or []
            role = next((getattr(r, "role", "") for r in lane_repos if getattr(r, "name", "") == repo), "")
            added = _ensure_alert_rules(services, snap, t, role)
            if added:
                notes.append(f"{t['component']}: alert rules {', '.join(added)}")
            try:
                rec = services.openchoreo.promote(component=t["component"], env=env, actor=f"agent:{stage}",
                                                  actor_type="agent", project=t["project"],
                                                  namespace=t["namespace"], pipeline=t["pipeline"])
                sized = _apply_capacity(services, snap, t, env)
                deps[repo] = {"repo": repo, **t, "pipeline": list(t["pipeline"]), "env": env,
                              "release": rec.get("release", ""), **({"capacity": sized} if sized else {})}
            except Exception as exc:
                failures.append({"repo": repo, "component": t["component"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        for repo, app in ((state.get("change_set") or {}).get("store") or {}).items():
            try:
                deps[repo] = release_store_app(services, state, repo, app["sha"], stage, actor=f"agent:{stage}")
            except Exception as exc:
                failures.append({"repo": repo, "component": repo, "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        if failures:
            res = {"status": "failure", "wait": stage, "results": failures, "at": now(),
                   "reason": f"Couldn't deploy to {stage}: " + "; ".join(f["message"] for f in failures)}
            return _people_said(state, stage, res, ok=nxt, retry=f"deploy_{stage}", extra={"deployments": deps})
        events = [event(f"deploy_{stage}", "deployed", ", ".join(sorted(deps)))] if deps else []
        events += [event(f"deploy_{stage}", "alert_rules", n) for n in notes]
        return Command(goto=f"{stage}_wait", update={"deployments": deps, "events": events})

    return deploy, (f"{stage}_wait", nxt, f"deploy_{stage}", END)


def make_env_wait(services, stage: str, ok: str, retry: str):
    def env_wait(state: FeatureState) -> Command:
        targets = [{"repo": d["repo"], "component": d["component"], "project": d.get("project"),
                    "namespace": d.get("namespace"), "env": d["env"],
                    **({"kind": "store", "sha": d.get("sha", "")} if d.get("kind") == "store" else {}),
                    **({"kind": "resource"} if d.get("kind") == "resource" else {})}
                   for d in (state.get("deployments") or {}).values()
                   if d.get("env") == env_for(stage, tuple(d.get("pipeline") or PIPELINE))]
        if not targets:
            return Command(goto=ok)
        runs = (state.get("change_set") or {}).get("runs") or {}
        roles = {t["repo"]: t.get("role") for t in state.get("tasks") or []}
        perf = state.get("performance") or {}
        qtargets = [{**t, "sha": t.get("sha") or runs.get(t["repo"], {}).get("sha", ""), "role": roles.get(t["repo"], ""),
                     "perf": (perf.get(t["repo"]) or {}).get("level", "none")}
                    for t in targets
                    # In production a store app is only submitted: its health is the rollout's to judge.
                    if not (t.get("kind") == "store" and stage == "production") and t.get("kind") != "resource"]
        qtargets += acceptance_targets(services, state, stage, [t for t in targets if t.get("kind") not in ("store", "resource")])
        from .. import screens as _screens

        qtargets += _screens.targets(services, state, stage)
        packet = external(state, stage, targets, quality_packet(services, state, stage, qtargets))
        if stage == "staging" and any(t.get("perf") == "full" for t in qtargets):
            # Staging is smaller than production: the load test runs at a share of the expected peak.
            s = services.feature_snapshot(state).product.performance or {}
            packet["performance"] = {"perf_settings": {
                "peak_rps": s.get("peak_rps"), "load_factor": s.get("staging_load_factor"),
                "regression_pct": s.get("staging_regression_pct"), "budgets": s.get("budgets"),
                # Staging is a fixed small size: with a capacity plan, each component is loaded to what
                # its staging pods should take, and the test reports what one pod handled (calibration).
                **({"staging_rps": _staging_targets(services.feature_snapshot(state))}
                   if getattr(services.feature_snapshot(state), "capacity", None) else {})}}
        if any(t.get("kind") == "store" for t in targets):
            packet["timeout_s"] = max(packet["timeout_s"], STORE_TIMEOUT_S[stage])
        res = interrupt(packet)
        report_to_github(services, state, stage, res)
        if res["status"] == "security":
            return fix_or_ask(services, state, stage, res, lambda: security_review(
                services, state, stage, res, ok=ok, retry=f"{stage}_wait"))
        if res["status"] == "success":
            # Staging passed: the next failure starts a fresh count of tries.
            reset = {"fix_retries": 0, "fix_branch": ""} if stage == "staging" else {}
            evs = [event(f"{stage}_wait", "passed", f"{len(targets)} components ready in {stage}")]
            if stage == "staging":
                # Did the events that measure the feature's goal fire? G6 shows the answer.
                reset["instrumentation_check"] = check = instrumentation_staging(services, state)
                if check.get("events"):
                    from ..outcomes.instrumentation import describe

                    evs.append(event("staging_wait", "instrumentation", describe(check),
                                     missing=list(check.get("missing") or [])))
                # Did every event published reach every consumer, with none breaking its contract?
                reset["datasets_check"] = recon = datasets_staging(services, state)
                if recon.get("datasets"):
                    from ..reconcile import describe as describe_datasets

                    evs.append(event("staging_wait", "datasets", describe_datasets(recon),
                                     problems=[p["message"] for p in recon.get("problems") or []]))
                if recon.get("problems"):  # holds the release, like a failed suite (decision 3)
                    held = {**res, "status": "failed", "reason": "the datasets don't reconcile in staging",
                            "results": [*(res.get("results") or []),
                                        *({"repo": p["repo"], "state": "failed", "message": p["message"]}
                                          for p in recon["problems"])]}
                    return fix_or_ask(services, state, stage, held, lambda: _people_said(
                        state, stage, held, ok=ok, retry=retry, extra={"datasets_check": recon}),
                        extra={"datasets_check": recon})
            return Command(goto=ok, update={"delivery": {stage: res}, **_quality_update(res), **reset, "events": evs})
        return fix_or_ask(services, state, stage, res, lambda: _people_said(
            state, stage, res, ok=ok, retry=retry, extra=_quality_update(res)))

    return env_wait, (ok, retry, f"{stage}_wait", "build", END)
