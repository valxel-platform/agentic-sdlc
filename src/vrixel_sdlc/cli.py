"""`vrixel demo`: run one feature through the whole pipeline in stub mode, answering gates
automatically, and print the timeline, gates and token usage."""

from __future__ import annotations

import argparse
import json
import tempfile

from langgraph.checkpoint.memory import InMemorySaver

from .config import load_settings
from .engine import FeatureEngine
from .services import build_services


def _demo(args: argparse.Namespace) -> int:
    settings = load_settings(mode="stub", artifact_dir=args.artifacts or tempfile.mkdtemp(prefix="vrixel-"))
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vrixel")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="Run one feature end to end with auto-approved gates")
    d.add_argument("--product", default="EMR", help="EMR, AG, POS, MS or APX")
    d.add_argument("--title", default="Consent check on patient intake")
    d.add_argument("--description", default="Block intake submission until patient consent is recorded")
    d.add_argument("--fault", action="append", default=[], metavar="PHASE=N",
                   help="Make the first N attempts of PHASE defective, e.g. spec=1")
    d.add_argument("--revise", action="store_true", help="Send the design back once at G3")
    d.add_argument("--artifacts", help="Directory for artifacts (default: a temp dir)")
    args = p.parse_args(argv)
    return _demo(args)


if __name__ == "__main__":
    raise SystemExit(main())
