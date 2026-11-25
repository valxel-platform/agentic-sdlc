"""Reconciliation between components, in staging (the design "Datasets, contracts and reporting").

Schemas prove each event has the right shape; this proves none went missing. Every template logs
one summary line a minute of the events it counted per dataset (counts only, never content):

    valxel.datasets {"emr.notes":{"published":12}}                      a producer (template-service)
    valxel.datasets {"emr.notes":{"applied":11,"duplicate":1}}          a consumer (event worker, knowledge)

After staging passes, the tool sums these per component since the staging deploy and checks every
data contract the design names: whatever its owner published, each consumer received (applied,
duplicate, skipped or parked), and nothing broke the contract (refused by the producer, parked by a
consumer). Anything else holds the release: the agents fix the repos it points at, and people decide
when the tries run out, as for any staging failure. In production the contract-broken alert rule
watches for the same breaks.

Connectors that pull from a source (integration.yaml `pull:`) also log, per source, how its runs
went (counts only):

    valxel.pulls {"test-catalog":{"runs":2,"succeeded":2,"failed":0,"seen":41,"published":40,"parked":1}}

For each source the HLD's Integrations line approves: a record parked (it broke its contract or
its handler) or a source that ran in staging and never succeeded holds the release. A source that
didn't run in staging isn't exercised, as for a dataset nobody published. What a pull published
reaches the valxel.datasets reconciliation above like any producer's events.
"""

from __future__ import annotations

import json

from .outcomes.instrumentation import _since

MARKER = "valxel.datasets"
PULLS = "valxel.pulls"
RECEIVED = ("applied", "duplicate", "skipped", "parked")


def parse(entries, marker: str = MARKER) -> dict[str, dict[str, dict[str, int]]]:
    """[(component, line)] -> component -> dataset (or pull source) -> outcome -> count, summed."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for component, text in entries:
        i = text.find(marker)
        if i < 0:
            continue
        try:
            rec = json.loads(text[i + len(marker):].strip())
        except ValueError:
            try:
                rec = json.loads(json.loads(text).get("message", "").split(marker, 1)[1].strip())
            except (ValueError, AttributeError, IndexError, TypeError):
                continue
        if not isinstance(rec, dict):
            continue
        mine = out.setdefault(str(component).split("/", 1)[-1], {})
        for dataset, outcomes in rec.items():
            if not isinstance(outcomes, dict):
                continue
            row = mine.setdefault(str(dataset), {})
            for outcome, n in outcomes.items():
                if isinstance(n, (int, float)):
                    row[str(outcome)] = row.get(str(outcome), 0) + int(n)
    return out


def problems(contracts: list[dict], counts: dict[str, dict[str, dict[str, int]]]) -> list[dict]:
    """What doesn't reconcile: [{"repo", "dataset", "message"}], for the repos that must fix it."""
    from .datasets import consumers, extensions

    out = []
    for c in contracts:
        dataset, owner = str(c.get("id")), str(extensions(c).get("owner") or "")
        short = owner.split("/", 1)[-1]
        sent = (counts.get(short) or {}).get(dataset) or {}
        published, refused = sent.get("published", 0), sent.get("refused", 0)
        if refused:
            out.append({"repo": owner, "dataset": dataset,
                        "message": f"refused {refused} {dataset} events that broke the data contract"})
        if not published:
            continue  # staging didn't exercise this dataset
        for repo in consumers(c):
            got = (counts.get(repo.split("/", 1)[-1]) or {}).get(dataset) or {}
            received = sum(got.get(k, 0) for k in RECEIVED)
            if got.get("parked"):
                out.append({"repo": repo, "dataset": dataset,
                            "message": f"parked {got['parked']} {dataset} events that broke the data contract"})
            if received < published:
                out.append({"repo": repo, "dataset": dataset,
                            "message": f"received {received} of the {published} {dataset} events {short} published"})
    return out


def pull_problems(sources: dict[str, list[str]], counts: dict[str, dict[str, dict[str, int]]]) -> list[dict]:
    """What a connector's pull runs in staging say must be fixed: [{"repo", "dataset", "message"}]
    ("dataset" holds the pull source)."""
    out = []
    for repo, names in sorted(sources.items()):
        mine = counts.get(repo.split("/", 1)[-1]) or {}
        for name in names:
            row = mine.get(name) or {}
            if row.get("parked"):
                out.append({"repo": repo, "dataset": name,
                            "message": f"pull {name} parked {row['parked']} of {row.get('seen', 0)} records "
                                       "(contract or handler)"})
            if row.get("runs") and not row.get("succeeded"):
                out.append({"repo": repo, "dataset": name,
                            "message": f"pull {name} failed all {row['runs']} of its runs in staging"})
    return out


def staging_check(services, state: dict, contracts: list[dict], environment: str,
                  pulls: dict[str, list[str]] | None = None) -> dict:
    """{"checked", "environment", "datasets", "pulls", "problems": [{"repo", "dataset", "message"}], "why"?}"""
    pulls = {r: list(n) for r, n in (pulls or {}).items() if n}
    base = {"checked": False, "environment": environment, "datasets": [str(c.get("id")) for c in contracts],
            "pulls": sorted(f"{r.split('/', 1)[-1]}/{n}" for r, ns in pulls.items() for n in ns), "problems": []}
    if not contracts and not pulls:
        return {**base, "why": "the design names no data contracts"}
    faults = int((state.get("simulate_faults") or {}).get("datasets", 0))
    logs = getattr(services, "cohort_logs", None)
    if logs is None:
        if faults:  # stub mode, asked to show a gap
            from .datasets import consumers

            c = contracts[0]
            reader = next(iter(sorted(consumers(c))), "")
            return {**base, "checked": True, "problems": [{"repo": reader, "dataset": str(c.get("id")),
                                                            "message": f"received 0 of the 3 {c.get('id')} events published"}]}
        return {**base, "why": "no log source to count events (set VALXEL_OPENCHOREO_OBSERVER_URL)"}
    try:
        from datetime import datetime, timezone

        rt = services.snapshot(state["product_id"]).product.runtime or {}
        until = datetime.now(timezone.utc)

        def read(phrase: str):
            return logs.entries(namespace=rt.get("namespace") or "default",
                                project=rt.get("openchoreo_project") or "", environment=environment,
                                since=_since(state), until=until, phrase=phrase)

        entries = read(MARKER) if contracts else []
        pulled = read(PULLS) if pulls else []
    except Exception as exc:  # noqa: BLE001 - reported at G6, not a failure of the feature
        return {**base, "why": f"couldn't read the logs: {type(exc).__name__}: {exc}"[:200]}
    found = problems(contracts, parse(entries)) + pull_problems(pulls, parse(pulled, PULLS))
    return {**base, "checked": True, "problems": found}


def describe(check: dict) -> str:
    if not check or not (check.get("datasets") or check.get("pulls")):
        return ""
    if not check.get("checked"):
        return f"Datasets not reconciled: {check.get('why', '')}"
    if check["problems"]:
        return "Datasets don't reconcile in staging: " + "; ".join(
            f"{p['repo'].split('/', 1)[-1]} {p['message']}" for p in check["problems"])
    parts = [f"{len(check['datasets'])} datasets"] if check.get("datasets") else []
    parts += [f"{len(check['pulls'])} pull sources"] if check.get("pulls") else []
    return f"All {' and '.join(parts)} reconcile in {check['environment']}"
