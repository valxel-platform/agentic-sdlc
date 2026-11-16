"""A feature's budget, split across its phases (the context design, C7).

No one phase can spend the whole feature budget (`budget_usd` in product.yaml): each group of
phases may spend up to its share, counted cumulatively, so whatever earlier phases didn't use
rolls forward. A phase that reaches its share escalates to a person (VX-10), who can add money;
what they add goes to the stuck phase and everything after it.

    cap(group) = budget - base * (1 - shares up to and including the group)

`base` is the budget the feature started with and `budget` is today's, base plus every amount
people added. Starting shares (product.yaml `budget_shares` overrides them, in percent) come from
the design and get reset from the ledger once 10 pilot features have run:

    documents  discovery, spec, design and architecture     15
    plan       the plan                                       5
    build      the first build, its reviews and their fixes  55
    fixes      builds again after CI failed                  20
    release    release and operate                            5
"""

from __future__ import annotations

GROUPS = ("documents", "plan", "build", "fixes", "release")
DEFAULT_SHARES = {"documents": 15.0, "plan": 5.0, "build": 55.0, "fixes": 20.0, "release": 5.0}
LABEL = {"documents": "documents (discovery to architecture)", "plan": "plan", "build": "build",
         "fixes": "CI fixes", "release": "release"}


def group_of(phase: str, state: dict | None = None) -> str:
    if phase in ("discovery", "spec", "design", "architecture"):
        return "documents"
    if phase == "plan":
        return "plan"
    if phase == "build":
        return "fixes" if (state or {}).get("rerun_repos") else "build"
    return "release"


def check_shares(raw) -> tuple[dict[str, float], list[str]]:
    """product.yaml's `budget_shares`: percentages for some or all groups, the rest from the
    defaults, together 100."""
    if raw in (None, {}):
        return dict(DEFAULT_SHARES), []
    if not isinstance(raw, dict):
        return dict(DEFAULT_SHARES), ["budget_shares must map phase groups to percentages"]
    problems = [f"budget_shares: unknown group {k} (one of {', '.join(GROUPS)})" for k in raw if k not in GROUPS]
    shares = dict(DEFAULT_SHARES)
    for k, v in raw.items():
        if k in GROUPS:
            try:
                shares[k] = float(v)
            except (TypeError, ValueError):
                problems.append(f"budget_shares.{k} must be a number")
                continue
            if shares[k] < 0:
                problems.append(f"budget_shares.{k} can't be negative")
    total = sum(shares.values())
    if not problems and abs(total - 100) > 0.01:
        problems.append(f"budget_shares add up to {total:g}%, not 100%")
    return shares, problems


def cap(group: str, *, budget: float, base: float, shares: dict[str, float] | None = None) -> float:
    """How much the feature may have spent in all, by the end of `group`."""
    shares = shares or DEFAULT_SHARES
    upto = sum(shares[g] for g in GROUPS[: GROUPS.index(group) + 1]) / 100.0
    base = min(base or budget, budget)
    return round(budget - base * (1.0 - upto), 6)


def over(phase: str, state: dict, *, spent: float, budget: float, base: float,
         shares: dict[str, float] | None = None) -> tuple[float, str]:
    """(what the phase may still spend, the escalation's reason when that's nothing)."""
    g = group_of(phase, state)
    limit = cap(g, budget=budget, base=base, shares=shares)
    left = limit - spent
    if left > 0:
        return left, ""
    if spent >= budget:
        return 0.0, f"token budget exhausted: ${spent:.2f} of ${budget:.2f} (VX-10)"
    pct = (shares or DEFAULT_SHARES)[g]
    return 0.0, (f"the {LABEL[g]} share of the budget is spent: ${spent:.2f} so far, and this phase may take the "
                 f"feature to ${limit:.2f} ({pct:g}% of ${min(base or budget, budget):.2f}, plus what earlier "
                 f"phases left and what people added). Add budget to go on (VX-10)")
