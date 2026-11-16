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
"""

from __future__ import annotations

import json

from .outcomes.instrumentation import _since

MARKER = "valxel.datasets"
RECEIVED = ("applied", "duplicate", "skipped", "parked")


def parse(entries) -> dict[str, dict[str, dict[str, int]]]:
    """[(component, line)] -> component -> dataset -> outcome -> count, summed."""
    out: dict[str, dict[str, dict[str, int]]] = {}
    for component, text in entries:
        i = text.find(MARKER)
        if i < 0:
            continue
        try:
            rec = json.loads(text[i + len(MARKER):].strip())
        except ValueError:
            try:
                rec = json.loads(json.loads(text).get("message", "").split(MARKER, 1)[1].strip())
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


def staging_check(services, state: dict, contracts: list[dict], environment: str) -> dict:
    """{"checked", "environment", "datasets", "problems": [{"repo", "dataset", "message"}], "why"?}"""
    base = {"checked": False, "environment": environment, "datasets": [str(c.get("id")) for c in contracts],
            "problems": []}
    if not contracts:
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
        entries = logs.entries(namespace=rt.get("namespace") or "default", project=rt.get("openchoreo_project") or "",
                               environment=environment, since=_since(state), until=datetime.now(timezone.utc),
                               phrase=MARKER)
    except Exception as exc:  # noqa: BLE001 - reported at G6, not a failure of the feature
        return {**base, "why": f"couldn't read the logs: {type(exc).__name__}: {exc}"[:200]}
    return {**base, "checked": True, "problems": problems(contracts, parse(entries))}


def describe(check: dict) -> str:
    if not check or not check.get("datasets"):
        return ""
    if not check.get("checked"):
        return f"Datasets not reconciled: {check.get('why', '')}"
    if check["problems"]:
        return "Datasets don't reconcile in staging: " + "; ".join(
            f"{p['repo'].split('/', 1)[-1]} {p['message']}" for p in check["problems"])
    return f"All {len(check['datasets'])} datasets reconcile in {check['environment']}"
