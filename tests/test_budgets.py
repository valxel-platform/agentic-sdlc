"""Phase shares of a feature's budget (the context design, C7)."""

from valxel_sdlc import budgets
from valxel_sdlc.budgets import DEFAULT_SHARES, cap, check_shares, group_of, over


def test_each_group_may_take_the_feature_to_its_cumulative_share():
    caps = {g: cap(g, budget=100, base=100) for g in budgets.GROUPS}
    assert caps == {"documents": 15, "plan": 20, "build": 75, "fixes": 95, "release": 100}


def test_what_earlier_phases_leave_rolls_forward():
    # Documents and plan spent $5 of their $20: the build may take the feature to $75, so $70 is its own.
    left, why = over("build", {}, spent=5, budget=100, base=100)
    assert left == 70 and why == ""


def test_a_phase_at_its_share_escalates_and_what_people_add_goes_to_it():
    left, why = over("build", {}, spent=75, budget=100, base=100)
    assert left == 0 and why.startswith("the build share of the budget is spent: $75.00 so far")
    # A person adds $30 (the budget becomes $130): all of it is the stuck phase's, and the rest keep theirs.
    assert over("build", {}, spent=75, budget=130, base=100)[0] == 30
    assert cap("fixes", budget=130, base=100) == 125 and cap("release", budget=130, base=100) == 130


def test_ci_fixes_have_their_own_share():
    assert group_of("build", {}) == "build" and group_of("build", {"rerun_repos": ["acme/x"]}) == "fixes"
    assert over("build", {"rerun_repos": ["acme/x"]}, spent=80, budget=100, base=100)[0] == 15
    assert [group_of(p) for p in ("discovery", "spec", "design", "architecture", "plan", "release", "operate")] == [
        "documents"] * 4 + ["plan", "release", "release"]


def test_the_whole_budget_spent_says_so():
    assert over("release", {}, spent=100, budget=100, base=100)[1] == "token budget exhausted: $100.00 of $100.00 (VX-10)"


def test_a_product_sets_its_own_shares():
    shares, problems = check_shares({"documents": 25, "build": 45})
    assert problems == [] and shares == {**DEFAULT_SHARES, "documents": 25.0, "build": 45.0}
    assert cap("documents", budget=200, base=200, shares=shares) == 50
    assert check_shares(None) == (DEFAULT_SHARES, [])
    assert check_shares({"build": 60})[1] == ["budget_shares add up to 105%, not 100%"]
    assert check_shares("lots")[1] == ["budget_shares must map phase groups to percentages"]
    assert check_shares({"build": "most"})[1][0] == "budget_shares.build must be a number"
