"""`valxel demo`: run one feature through the whole pipeline in stub mode, answering gates
automatically, and print the timeline, gates and token usage.

`valxel repos check|apply`: compare every managed repo with the standards in config/repos.yaml
and report (check) or fix (apply) what differs. Uses the GitHub App (VALXEL_GITHUB_APP_ID,
VALXEL_GITHUB_APP_PRIVATE_KEY_FILE) with administration rights, and reads governance from GitHub
(VALXEL_GOVERNANCE_SOURCE=github) or the local examples.

`valxel acceptance review`: list acceptance tests whose coverage another test of the same
capability already has, from a product's catalog.json, so they can be merged or removed."""

from __future__ import annotations

import argparse
import os
import json
import sys
import tempfile

from langgraph.checkpoint.memory import InMemorySaver

from .config import load_settings
from .engine import FeatureEngine
from .services import build_services


def _demo(args: argparse.Namespace) -> int:
    settings = load_settings(mode="stub", artifact_dir=args.artifacts or tempfile.mkdtemp(prefix="valxel-"))
    engine = FeatureEngine(build_services(settings), InMemorySaver())
    faults = dict(f.split("=") for f in args.fault)
    view = engine.start(product_id=args.product, title=args.title, description=args.description,
                        simulate_faults={k: int(v) for k, v in faults.items()})
    decisions = {"g3": {"decision": "revise", "feedback": "Add an empty state for first-time users"}} if args.revise else None
    view = engine.auto_drive(view["feature_id"], decisions=decisions)

    print(f"\n{view['feature_id']} · {view['title']} · status: {view['status']} · risk: {view['risk_tier']}")
    print(f"constitution {view['constitution_version']}\n")
    print("Timeline")
    for e in view["events"]:
        print(f"  {e['node']:<22} {e['kind']:<34} {e.get('detail', '')[:60]}")
    print("\nHuman decisions")
    for g in view["gate_decisions"]:
        print(f"  {g['gate']:<6} {g.get('lane', ''):<6} {g['decision']:<10} by {g['approver']}")
    print("\nArtifacts:", ", ".join(view["artifacts"].values()))
    print("\nUsage (stub provider, list prices)")
    print(json.dumps(view["usage"], indent=2))
    print(f"\nArtifacts written to {settings.artifact_dir}")
    return 0 if view["status"] == "done" else 1


def _repos(args: argparse.Namespace, github=None) -> int:
    from . import repo_standards

    settings = load_settings(mode="stub", database_url="")
    services = build_services(settings)
    gh = github or _admin_github(settings)
    reports = repo_standards.run(services, gh, apply=args.action == "apply", only=args.repo or None)
    if args.json:
        print(json.dumps([r.as_dict() for r in reports], indent=2, default=str))
    else:
        print(repo_standards.summary(reports))
    return 0 if all(r.ok for r in reports) else 1


def _acceptance(args: argparse.Namespace, github=None) -> int:
    from pathlib import Path

    from . import acceptance

    if args.catalog:
        text = Path(args.catalog).read_text()
    else:
        settings = load_settings(mode="stub", database_url="")
        services = build_services(settings)
        product = services.governance.snapshot(args.product).product
        repo = acceptance.acceptance_repo([{"name": r.name, "role": r.role} for r in product.repos])
        if not repo:
            raise SystemExit(f"{args.product} has no acceptance repo (role: acceptance in its product.yaml)")
        text = (github or _admin_github(settings)).get_file(repo=repo, path="catalog.json", ref="")
    catalog = acceptance.load_catalog(text)
    pairs = acceptance.overlaps_in(catalog)
    if args.json:
        print(json.dumps({"tests": len(catalog), "overlaps": pairs}, indent=2))
    else:
        print(f"{len(catalog)} acceptance tests, {len(pairs)} covered by another test")
        for p in pairs:
            print(f"  {p['test']}\n    is within {p['within']}\n    covers {', '.join(p['covers'])}")
    return 1 if pairs else 0


def _admin_github(settings):
    import os
    from pathlib import Path

    from .governance.sources import GitHubAppTokenProvider
    from .integrations.github import PROVISIONING_PERMISSIONS, GitHubRest

    missing = [v for v in ("VALXEL_GITHUB_APP_ID", "VALXEL_GITHUB_APP_PRIVATE_KEY_FILE") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"valxel repos needs the GitHub App: set {', '.join(missing)}")
    tokens = GitHubAppTokenProvider(os.environ["VALXEL_GITHUB_APP_ID"],
                                    Path(os.environ["VALXEL_GITHUB_APP_PRIVATE_KEY_FILE"]).read_text(),
                                    api=settings.github_api, permissions=PROVISIONING_PERMISSIONS)
    return GitHubRest(tokens, api=settings.github_api)


def _verify_changes(log=None) -> int:
    """Exit 0 when every record chains to the one before it, 1 when the log was altered."""
    from .ops.changes import PostgresChangeLog, verify

    if log is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            print("Set DATABASE_URL to the tool's database.", file=sys.stderr)
            return 2
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(url, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        log = PostgresChangeLog(pool)
    out = verify(log.all())
    if out["ok"]:
        print(f"Change log intact: {out['records']} records.")
        return 0
    print(f"Change log altered: the chain breaks at {out['broken_at']} ({out['records']} records).", file=sys.stderr)
    return 1


def _pilot_report(args, engine=None) -> int:
    from .pilot import markdown, report

    if engine is None:
        if not os.environ.get("DATABASE_URL"):
            print("Set DATABASE_URL to the tool's database.", file=sys.stderr)
            return 2
        from .api.app import create_engine_from_env

        engine, _ = create_engine_from_env()
    rep = report(engine, args.product, pilot_only=not args.all, since=args.since)
    print(json.dumps(rep, indent=2) if args.json else markdown(rep))
    return 0


def _eval(args: argparse.Namespace) -> int:
    """Export eval cases, or replay them under the config in use and a candidate and compare."""
    import os
    from pathlib import Path

    from . import evals

    settings = load_settings(mode=args.mode)
    phases = tuple(args.phase or ())
    classes = tuple(args.data_class or ("general",))
    if args.cases:
        cases = [c for c in evals.load_jsonl(args.cases)
                 if (not phases or c.phase in phases) and c.data_class in classes and (not args.product or c.product_id == args.product)]
        cases = evals._select(cases, args.product, phases, classes, True, args.limit)
    else:
        if not os.environ.get("DATABASE_URL"):
            raise SystemExit("valxel eval reads cases from the tool's database (DATABASE_URL), or give --cases FILE")
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(os.environ["DATABASE_URL"], max_size=2, kwargs={"autocommit": True, "row_factory": dict_row})
        cases = evals.PostgresEvalStore(pool).cases(product_id=args.product, phases=phases, data_classes=classes,
                                                     limit_per_phase=args.limit)
    if args.action == "export":
        evals.dump_jsonl(cases, args.out or "cases.jsonl")
        print(f"{len(cases)} cases written to {args.out or 'cases.jsonl'} (data classes: {', '.join(classes)})")
        return 0
    if not cases:
        print("No labelled cases to run.", file=sys.stderr)
        return 2
    base = evals.run(evals.services_for(settings, args.baseline_config), cases, pairwise=args.pairwise,
                     budget_usd=args.budget)
    cand = evals.run(evals.services_for(settings, args.config), cases, pairwise=args.pairwise, budget_usd=args.budget)
    cmp = evals.compare(base, cand, max_drop=args.max_drop, max_cost_increase=args.max_cost_increase)
    if args.out:
        Path(args.out).write_text(json.dumps({"comparison": cmp, "baseline": base, "candidate": cand}, indent=2, default=str))
    lf = evals.Langfuse.from_env()
    if lf is not None:
        lf.sync(cases, f"candidate-{Path(args.config).name}", cand["results"], content_classes=classes)
    print(evals.markdown(cmp, base, cand))
    return 0 if cmp["ok"] else 1


def _inception(args) -> int:
    """Write the next inception documents to --out; exit 1 when one couldn't pass its checks."""
    import json
    from pathlib import Path

    from .config import load_settings
    from .inception import KIND, Vision
    from .inception.run import draft as draft_doc
    from .inception.run import research as research_note
    from .inception.run import run
    from .services import build_services

    settings = load_settings(mode=args.mode)
    services = build_services(settings)
    existing = bool(args.product and args.product in settings.registry)
    if not args.vision and not existing:
        print("Give --vision (a new product) or --product with a registered product id.", file=sys.stderr)
        return 2
    text = Path(args.vision).read_text(encoding="utf-8") if args.vision else f"# {args.product}\n"
    org = args.org or (settings.entry(args.product).governance.org if existing else "")
    if not org:
        print("Give --org: the GitHub org the product will live in.", file=sys.stderr)
        return 2
    vision = Vision.from_markdown(text, org=org, product_id=args.product, existing=existing)
    vision.research_depth = args.research
    brief = Path(args.brief).read_text(encoding="utf-8") if args.brief else ""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = []
    if brief:
        drafts = run(services, vision, brief=brief, through=args.through)
    else:  # research first, then the brief that cites it
        r = research_note(services, vision)
        path = out / f"{KIND['research']}.md"
        path.write_text(r.text, encoding="utf-8")
        report.append({k: v for k, v in r.as_dict().items() if k != "text"} | {"doc": "research", "file": str(path)})
        print(f"research: {r.status} ({r.depth}, {'searched the web' if r.web else 'no web search'}), "
              f"${r.cost_usd:.2f} -> {path}")
        for f in r.findings:
            print(f"  - {f}")
        drafts = [draft_doc(services, vision, "brief", research=r.text)]
    for d in drafts:
        path = out / f"{KIND[d.doc]}.md"
        path.write_text(d.text, encoding="utf-8")
        report.append({k: v for k, v in d.as_dict().items() if k != "text"} | {"file": str(path)})
        print(f"{d.doc}: {d.status} after {d.attempts} attempt(s), ${d.cost_usd:.2f} -> {path}")
        for f in d.findings:
            print(f"  - {f}")
        if d.monthly_usd:
            print("  monthly cost: " + ", ".join(f"{e} ${v:,.0f}" for e, v in d.monthly_usd.items()))
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not brief and drafts and drafts[0].status == "passed":
        print(f"Next: the brief gate. Once approved, run again with --brief {out / 'BRIEF.md'}")
    return 0 if all(d.status == "passed" for d in drafts) else 1


def _outcomes(args, services=None) -> int:
    """Check a product's goal metrics, read one, or record a number typed in by hand."""
    import json

    from .config import load_settings
    from .outcomes import read as outcomes
    from .services import build_services

    if services is None:
        settings = load_settings()
        services = build_services(settings)
        if settings.database_url:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            from .outcomes.store import PostgresReadingStore

            pool = ConnectionPool(settings.database_url, max_size=2,
                                  kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
            services.readings = PostgresReadingStore(pool)
        elif args.action == "record":
            print("Recording needs the tool's database: set DATABASE_URL.", file=sys.stderr)
            return 2
    if args.action == "check":
        rows = outcomes.check(services, args.product)
        if args.json:
            print(json.dumps(rows, indent=2))
        for r in rows if not args.json else []:
            m = r["metric"]
            print(f"{r['goal_id']} {r['title']}: " + (f"{m['source']}, {m['direction']} is better" if m else "no metric"))
            for problem in r["problems"]:
                print(f"  - {problem}")
        return 1 if any(r["problems"] for r in rows) else 0
    if not args.goal:
        print("Name the goal, e.g. GOAL-CLN-1.", file=sys.stderr)
        return 2
    if args.start and args.end:
        start, end = args.start, args.end
    else:
        start, end = outcomes.window(args.days, args.end)
    try:
        if args.action == "record":
            if args.value is None:
                print("Give --value.", file=sys.stderr)
                return 2
            reading = outcomes.record_manual(services, args.product, args.goal, value=args.value, n=args.n,
                                             start=start, end=end, entered_by=args.by)
        else:
            reading = outcomes.read(services, args.product, args.goal, start=start, end=end, record=not args.dry_run)
    except (KeyError, ValueError) as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(reading.as_dict(), indent=2))
    else:
        cfg, _ = outcomes.product_metrics(services, args.product)
        metric = next(r["metric"] for r in outcomes.check(services, args.product) if r["goal_id"] == args.goal)
        print(f"{args.goal} {start[:10]} to {end[:10]} ({reading.source}): "
              f"{reading.shown((metric or {}).get('unit', ''), cfg['min_cell'])}")
        if reading.note and reading.status == "ok":
            print(f"  {reading.note}")
    return 0 if reading.status in ("ok", "suppressed", "no_data") else 1


def main(argv: list[str] | None = None, *, github=None) -> int:
    p = argparse.ArgumentParser(prog="valxel")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="Run one feature end to end with auto-approved gates")
    d.add_argument("--product", default="CLN", help="CLN, FARM, SHOP, DNT or PAY")
    d.add_argument("--title", default="Consent check on patient intake")
    d.add_argument("--description", default="Block intake submission until patient consent is recorded")
    d.add_argument("--fault", action="append", default=[], metavar="PHASE=N",
                   help="Make the first N attempts of PHASE defective, e.g. spec=1")
    d.add_argument("--revise", action="store_true", help="Send the design back once at G3")
    d.add_argument("--artifacts", help="Directory for artifacts (default: a temp dir)")
    r = sub.add_parser("repos", help="Check or apply the repo standards (config/repos.yaml) on every managed repo")
    r.add_argument("action", choices=["check", "apply"])
    r.add_argument("--repo", action="append", default=[], metavar="ORG/REPO", help="Only these repos")
    r.add_argument("--json", action="store_true", help="Print the reports as JSON")
    a = sub.add_parser("acceptance", help="Review a product's acceptance tests for overlap")
    a.add_argument("action", choices=["review"])
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("--product", help="Product id; reads catalog.json from its acceptance repo (GitHub App)")
    src.add_argument("--catalog", help="A local catalog.json")
    a.add_argument("--json", action="store_true")
    g = sub.add_parser("glitchtip-token", help="The token for a product's GlitchTip webhook URL")
    g.add_argument("product")
    c = sub.add_parser("changes", help="Check the production change log's hash chain (DATABASE_URL)")
    c.add_argument("action", choices=["verify"])
    pr = sub.add_parser("pilot-report", help="The pilot's numbers for a product, as Markdown (DATABASE_URL)")
    pr.add_argument("product")
    pr.add_argument("--all", action="store_true", help="Every feature, not only those created as pilot features")
    pr.add_argument("--since", default="", help="Only features started on or after this date (YYYY-MM-DD)")
    pr.add_argument("--json", action="store_true")
    e = sub.add_parser("eval", help="Export eval cases, or replay them under a candidate config and compare")
    e.add_argument("action", choices=["export", "run"])
    e.add_argument("--cases", help="A JSONL file of cases (default: the tool's database, DATABASE_URL)")
    e.add_argument("--product", help="Only this product's cases")
    e.add_argument("--phase", action="append", help="Only these phases (repeatable)")
    e.add_argument("--data-class", action="append",
                   help="Data classes to include (default general; regulated cases only go to their allowed providers)")
    e.add_argument("--limit", type=int, default=20, help="Newest cases per phase (default 20)")
    e.add_argument("--config", default="config", help="The candidate config directory")
    e.add_argument("--baseline-config", default=None, help="The config to compare with (default: the one in use)")
    e.add_argument("--pairwise", action="store_true", help="Also judge each new document against the approved one")
    e.add_argument("--max-drop", type=float, default=0.05, help="Largest drop in a rate that isn't a regression")
    e.add_argument("--max-cost-increase", type=float, default=None, help="e.g. 0.25 fails a run that costs 25%% more")
    e.add_argument("--budget", type=float, default=None, help="Dollars each config's run may spend")
    e.add_argument("--mode", default="live", choices=["live", "stub"])
    e.add_argument("--out", help="Write cases (export) or the full results (run) here")
    inc = sub.add_parser("inception", help="Draft a product's brief, then (with the approved brief) its "
                                           "architecture and roadmap")
    inc.add_argument("--vision", help="A Markdown vision: '# Name', the idea, then optional '## Goals', '## Users', "
                                      "'## Constraints' and '## Budget' sections")
    inc.add_argument("--org", default="", help="The product's GitHub org (it must exist)")
    inc.add_argument("--product", default="", help="An existing product's id (describe it), or a proposed id")
    inc.add_argument("--brief", help="The brief approved at the brief gate: drafts the architecture and roadmap")
    inc.add_argument("--through", choices=["architecture", "roadmap"], default="roadmap")
    inc.add_argument("--research", choices=["quick", "thorough", "own"], default="quick",
                     help="Before the brief: a quick or thorough web search, or 'own' (your research is in the vision)")
    inc.add_argument("--out", default="inception", help="Directory for the documents and report.json")
    inc.add_argument("--mode", default="stub", choices=["live", "stub"])
    dr = sub.add_parser("design-record", help="Draft a product's design record (design/ in its governance repo) "
                                              "from its web and mobile apps, as a pull request")
    dr.add_argument("--product", required=True)
    dr.add_argument("--mode", default="stub", choices=["live", "stub"])
    en = sub.add_parser("environments", help="Create a product's OpenChoreo project, environments and pipeline "
                                             "once the platform has registered its data planes")
    en.add_argument("--product", required=True)
    en.add_argument("--approver", required=True, help="Who approved the product gate (production is created for them)")
    en.add_argument("--mode", default="stub", choices=["live", "stub"])
    gl = sub.add_parser("go-live", help="Move a hosted lite product to the customer's VM (dev, staging and production), "
                                        "or retire its hosted environments once it is live")
    gl.add_argument("--product", required=True)
    gl.add_argument("--by", default=os.environ.get("USER", ""), help="Who decided to go to production (a person)")
    gl.add_argument("--vm", help="The non-production VM's Docker host, e.g. ssh://valxel@10.0.1.5")
    gl.add_argument("--prod-vm", default="", help="Production's own VM, if it has one")
    gl.add_argument("--retire", action="store_true", help="Remove the hosted dev and staging (after the PR merged)")
    gl.add_argument("--mode", default="stub", choices=["live", "stub"])
    ca = sub.add_parser("capacity", help="Propose a production stage step, or review production against the "
                                         "capacity plan, as a pull request to runtime/capacity.yaml")
    ca.add_argument("--product", required=True)
    ca.add_argument("--step", choices=["pilot", "growth", "scale"], help="Propose moving production to this stage")
    ca.add_argument("--reason", default="", help="--step: why (a contract's availability promise, a trigger)")
    ca.add_argument("--observed", default="", help="A JSON file of production's busy hour per component, to review")
    ca.add_argument("--mode", default="stub", choices=["live", "stub"])
    oc = sub.add_parser("outcomes", help="Check a product's goal metrics, read one for a window, or record a "
                                         "number typed in by hand")
    oc.add_argument("action", choices=["check", "read", "record"])
    oc.add_argument("product")
    oc.add_argument("goal", nargs="?", default="", help="e.g. GOAL-CLN-1 (read, record)")
    oc.add_argument("--days", type=int, default=30, help="The window: the last N days (default 30)")
    oc.add_argument("--start", default="", help="Window start (ISO date or time); with --end instead of --days")
    oc.add_argument("--end", default="", help="Window end (default now)")
    oc.add_argument("--value", type=float, help="record: the number")
    oc.add_argument("--n", type=int, help="record: how many it counts (required for PHI and payments products)")
    oc.add_argument("--by", default=os.environ.get("USER", ""), help="record: who entered it")
    oc.add_argument("--dry-run", action="store_true", help="read: don't record the reading")
    oc.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "outcomes":
        return _outcomes(args)
    if args.cmd == "inception":
        return _inception(args)
    if args.cmd == "environments":
        from .config import load_settings
        from .environments import environments
        from .services import build_services

        services = build_services(load_settings(mode=args.mode))
        try:  # a lite product is hosted on the shared plane until it goes live
            mode = (services.snapshot(args.product).product.runtime or {}).get("mode") or "full"
        except Exception:
            mode = "full"
        out = environments(services, product_id=args.product, approver=args.approver, mode=mode)
        print(out.get("why") or f"Created for {out['approved_by']}: " + ", ".join(f"{k} {v}" for k, v in out["created"].items()))
        return 0 if not out.get("waiting") else 2
    if args.cmd == "go-live":
        from .config import load_settings
        from .lite.golive import go_live, retire_hosted
        from .services import build_services

        services = build_services(load_settings(mode=args.mode))
        if args.retire:
            print("Removed " + ", ".join(retire_hosted(services, product_id=args.product)["removed"]))
            return 0
        if not args.vm:
            p.error("go-live needs --vm")
        out = go_live(services, product_id=args.product, approver=args.by,
                      vms={"non_production": args.vm, **({"production": args.prod_vm} if args.prod_vm else {})})
        for st in out["steps"]:
            print(f"{'ok ' if st['ok'] else 'FAILED'} {st['step']}: {st.get('error') or st.get('url') or ''}".rstrip(": "))
        return 0 if out["ok"] else 1
    if args.cmd == "capacity":
        return _capacity(args)
    if args.cmd == "design-record":
        from .config import load_settings
        from .design_record import draft_for_existing
        from .services import build_services

        out = draft_for_existing(build_services(load_settings(mode=args.mode)), args.product)
        print(f"Design record drafted: {', '.join(out['files'])} -> {out['pr']['url']}")
        for repo, sha in out["read"].items():
            print(f"  read {repo} @ {sha[:7]}")
        return 0
    if args.cmd == "eval":
        return _eval(args)
    if args.cmd == "pilot-report":
        return _pilot_report(args)
    if args.cmd == "changes":
        return _verify_changes()
    if args.cmd == "glitchtip-token":
        from .ops.error_tracking import token_for

        token = token_for(args.product)
        if not token:
            print("Set VALXEL_SIGNALS_TOKEN first (the tool's signals token).", file=sys.stderr)
            return 2
        print(f"/api/signals/glitchtip/{args.product}?token={token}")
        return 0
    if args.cmd == "repos":
        return _repos(args, github)
    if args.cmd == "acceptance":
        return _acceptance(args, github)
    return _demo(args)


if __name__ == "__main__":
    raise SystemExit(main())


def _capacity(args) -> int:
    import json

    from . import capacity
    from .config import load_settings
    from .services import build_services

    services = build_services(load_settings(mode=args.mode))
    cap = services.snapshot(args.product).capacity
    if not cap:
        print(f"{args.product} has no runtime/capacity.yaml yet")
        return 1
    if args.step:
        if not args.reason:
            print("Say why (--reason)")
            return 1
        new = capacity.with_stage(cap, args.step)
        pr = capacity.propose(services, args.product, new, branch=f"valxel/capacity-{args.step}",
                              title=f"{args.product}: production to the {args.step} stage",
                              why=(f"From {cap.get('stage', 'pilot')} to {args.step}: {args.reason}\n\nCompute at the "
                                   f"stage's minimums: about ${capacity.stage_cost(cap, cap.get('stage', 'pilot')):,.0f} -> "
                                   f"${capacity.stage_cost(new, args.step):,.0f} a month (databases as the plan sizes them)."))
        print(pr["url"] if pr else "Already at that stage")
        return 0
    observed = json.load(open(args.observed)) if args.observed else {}
    found = capacity.review(cap, observed)
    for f in found:
        print(f"{f['repo']}: {f['kind']}{' to ' + f['to'] if f.get('to') else ''}: {f['why']}")
    if not found:
        print("Production fits its stage: nothing to propose")
    return 0
