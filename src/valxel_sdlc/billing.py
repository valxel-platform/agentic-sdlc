"""Plans, the hosted trial, and billing at the first charge (the sign-in design, A9).

A founder picks a plan while creating the workspace and can change it later. A card is asked for
only when the plan first costs something:

    Self-hosted  $0, for organizations under 250 people and $10M revenue (the license's free-use
                 grant). No hosted workspace: Valxel shows the install guide instead. GitHub sign-in.
    Lite         founders at the pilot stage. Hosted dev and staging are free for 90 days; then the
                 plan's monthly price, or about $79 a month to extend hosting. Card at go-live, or
                 when extending. Valxel's shared plane, then one VM in their AWS account.
    Full         products past the pilot. The plan's monthly price per product. Card at go-live.
                 Valxel's shared plane, then two EKS clusters in their AWS account.

The payment provider and the Lite and Full prices are still to be chosen. Until then the provider is
`NoBilling`: nothing is charged and nothing is blocked; the workspace's billing page says so, and
says when a card will be asked for. A provider is a `BillingProvider` (has a card on file; the link
to add one); with one set (VALXEL_BILLING), connecting AWS for go-live waits for a card.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

TRIAL_DAYS = 90
INSTALL_GUIDE = "https://github.com/valxel-platform/agentic-sdlc#quick-start"


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    for_whom: str
    price: str
    card_when: str
    sign_in: str
    runs_on: str
    monthly_usd: float | None = None  # None: not set yet
    hosted: bool = True


def _price(env_name: str, default: float | None) -> float | None:
    raw = os.environ.get(env_name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def plans() -> list[Plan]:
    """The plans table. Monthly prices come from VALXEL_PRICE_LITE_USD and VALXEL_PRICE_FULL_USD once
    they're set; until then the page says the price is still to be announced."""
    lite, full = _price("VALXEL_PRICE_LITE_USD", None), _price("VALXEL_PRICE_FULL_USD", None)
    return [
        Plan("self-hosted", "Self-hosted", "Organizations under 250 people and $10M revenue (the license's free-use grant)",
             "$0", "Never", "GitHub", "Your own infrastructure: you install Valxel yourself", 0.0, hosted=False),
        Plan("lite", "Lite", "Founders at the pilot stage",
             f"Hosted dev and staging free for {TRIAL_DAYS} days; then "
             + (f"${lite:g} a month" if lite is not None else "the plan's monthly price (to be announced)")
             + ", or about $79 a month to extend hosting",
             "At go-live, or when extending hosting", "Google, email or GitHub",
             "Valxel's shared plane, then one VM in your AWS account", lite),
        Plan("full", "Full", "Products past the pilot",
             (f"${full:g} a month per product" if full is not None else "A monthly price per product (to be announced)"),
             "At go-live", "Google, email or GitHub", "Valxel's shared plane, then two EKS clusters in your AWS account",
             full),
    ]


def plan_of(plan_id: str) -> Plan:
    by_id = {p.id: p for p in plans()}
    return by_id.get(plan_id, by_id["lite"])


class BillingProvider(Protocol):
    name: str

    def has_card(self, workspace: str) -> bool: ...

    def add_card_url(self, workspace: str, *, return_to: str) -> str: ...


class NoBilling:
    """No payment provider yet: nothing is charged and nothing waits for a card."""

    name = "none"

    def has_card(self, workspace: str) -> bool:
        return False

    def add_card_url(self, workspace: str, *, return_to: str) -> str:
        return ""


def provider_for(env=None) -> BillingProvider:
    env = env if env is not None else os.environ
    kind = (env.get("VALXEL_BILLING") or "none").lower()
    if kind != "none":
        raise ValueError(f"VALXEL_BILLING={kind}: no payment provider is built in yet; leave it unset")
    return NoBilling()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def status(ws: dict, provider: BillingProvider, *, live: bool, today: datetime | None = None) -> dict:
    """What a workspace's billing page shows: the plan, the trial, and whether a card is needed now.
    `live`: the workspace has gone live (an AWS account connected)."""
    today = today or datetime.now(UTC)
    plan = plan_of(ws.get("plan", "lite"))
    out = {"plan": asdict(plan), "provider": provider.name, "card_on_file": provider.has_card(ws["id"]),
           "trial_ends": "", "trial_days_left": None, "card_needed": False, "note": ""}
    if plan.id == "lite" and ws.get("created"):
        ends = _parse(ws["created"]) + timedelta(days=TRIAL_DAYS)
        out["trial_ends"] = ends.date().isoformat()
        out["trial_days_left"] = max(0, (ends - today).days)
    out["card_needed"] = plan.hosted and plan.id != "self-hosted" and live and not out["card_on_file"]
    if provider.name == "none":
        out["note"] = ("Billing isn't switched on yet: nothing is charged. A card will be asked for "
                       + plan.card_when.lower() + ", once prices are set.")
        out["card_needed"] = False
    return out


def blocks_go_live(ws: dict, provider: BillingProvider) -> str:
    """Why connecting AWS (go-live) must wait for a card, or ""."""
    if provider.name == "none":
        return ""
    plan = plan_of(ws.get("plan", "lite"))
    if plan.hosted and not provider.has_card(ws["id"]):
        return f"Add a card before going live on the {plan.name} plan"
    return ""
