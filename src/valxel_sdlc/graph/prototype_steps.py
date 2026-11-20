"""Between the UX spec and G3: today's screens, then the prototype (T-90, T-91).

    design (the UX spec passed) -> design_prototype: ask the product's scan for today's screens and
    wait for it (valxel_sdlc.current_screens), then draw the prototype on top of them
    (valxel_sdlc.prototype) -> g3

    architecture -> architecture_done -> g4: a step that only keeps the two branches in step. The
    graph runs in rounds and a gate waiting for people holds its round, so G4 must open in the same
    round as G3, as it did when the design phase drew the pictures itself.

One step for both, so the drawing happens once: before the wait it hasn't started, and after the
wait nothing else in the step can pause it again.

Neither step holds the feature up: no screens to visit, nowhere to scan from, or no report within
20 minutes, and the prototype is drawn from the spec alone; a prototype that can't be drawn leaves
G3 with the spec and says why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langgraph.types import Command, interrupt

from ..state import FeatureState
from .phase import event, feature_ctx

REUSE_H = 24  # today's screens of the same routes, scanned this recently, are still today's


def now() -> str:
    return datetime.now(UTC).isoformat()


def make_architecture_done(services):
    def architecture_done(state: FeatureState) -> Command:
        return Command(goto="g4")

    return architecture_done, ("g4",)


def today(services, state: FeatureState) -> tuple[dict, list[dict]]:
    """Today's screens for this UX spec: (the state's today_screens, events). Waits for the scan."""
    from .. import current_screens
    from ..waits import LABELS

    routes = sorted({s["route"] for s in current_screens.wanted(services, state)})
    prev = state.get("today_screens") or {}
    fresh = (datetime.now(UTC) - timedelta(hours=REUSE_H)).isoformat()
    if prev.get("screens") and prev.get("routes") == routes and str(prev.get("at", "")) >= fresh:
        return prev, []  # a G3 revision: the app hasn't changed since
    scan = current_screens.plan(services, state) if routes else None
    if scan is None:
        why = "no web screens with routes" if not routes else "no repo to scan from or nothing in staging yet"
        return ({"routes": routes, "screens": {}, "at": now(), "skipped": why},
                [event("design_prototype", "skipped", f"Today's screens: {why}; drawing from the spec")])
    res = interrupt({"type": "external", "wait": "today_screens", "label": LABELS["today_screens"],
                     "feature_id": state["feature_id"], "product_id": state["product_id"],
                     "title": state["title"], "scan": scan, "since": now()})
    ok = (res or {}).get("status") == "success"
    shots = current_screens.read(res.get("summary", "")) if ok else {}
    seen = [sid for sid, s in shots.items() if s.get("today")]
    detail = (f"{len(seen)} of {len(scan['screens'])} screen(s) as they are today" if ok
              else (res or {}).get("reason", "no report"))
    return ({"repo": scan["repo"], "routes": routes, "screens": shots, "at": now(),
             "url": (res or {}).get("url", ""), **({} if ok else {"skipped": detail})},
            [event("design_prototype", "today_screens" if ok else "skipped", f"Today's screens: {detail}")])


def make_design_prototype(services):
    def design_prototype(state: FeatureState) -> Command:
        from .. import budgets, current_screens, prototype

        arts = state.get("artifacts") or {}
        if "UX" not in arts:
            return Command(goto="g3")
        today_state, said = today(services, state)
        product = services.feature_snapshot(state).product
        spent = float(state.get("cost_usd", 0.0))
        budget = float(state.get("token_budget_usd") or product.budget_usd)
        left, reason = budgets.over("design", state, spent=spent, budget=budget,
                                    base=float(state.get("budget_base_usd") or budget), shares=product.budget_shares)
        if reason:
            return Command(goto="g3", update={"today_screens": today_state, "events": said, "mockups": {
                "screens": [], "findings": [f"The prototype wasn't drawn: {reason}"]}})
        ctx = feature_ctx(services, state, "design")
        ctx["budget_remaining_usd"] = left
        ux = services.artifacts.load(arts["UX"])
        today_ = dict(today_state or {})
        screens = current_screens.wanted(services, state)
        try:
            m = prototype.make(services, state, ctx, ux, budget_usd=left,
                               images=current_screens.images(services, today_, screens),
                               today=current_screens.instruction(today_, screens))
        except Exception as exc:  # noqa: BLE001 - G3 still has the spec; say why there's nothing to click through
            return Command(goto="g3", update={"today_screens": today_state, "events": said, "mockups": {
                "screens": [], "findings": [f"The prototype couldn't be drawn: {type(exc).__name__}"]}})
        shots = today_.get("screens") or {}
        for s in m["mockups"]["screens"]:  # what G3 shows beside each screen: today's, or that it's new
            shot = shots.get(s["id"]) or {}
            s["today"] = {k: bool(shot.get(k)) for k in ("desktop", "phone")} if shot.get("today") else None
            s["new"] = bool(shots) and not shot.get("today")
        return Command(goto="g3", update={
            "artifacts": m["artifacts"], "cost_usd": m["cost_usd"], "tokens_used": m["tokens_used"],
            "mockups": m["mockups"], "today_screens": today_state,
            "events": said + [event("design_prototype", "prototype", f"a prototype of {len(m['mockups']['screens'])} screen(s)"
                             + (" on today's screens" if any(s.get("today") for s in m["mockups"]["screens"]) else ""),
                             findings=m["mockups"]["findings"])]})

    return design_prototype, ("g3",)
