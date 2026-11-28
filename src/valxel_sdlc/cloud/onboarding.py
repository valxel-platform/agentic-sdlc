"""Getting a new product's cloud accounts ready, asked at inception (the front door's product flow).

Every product runs on the installation's cloud (ADR 0010), in accounts of its own: one for
non-production (dev, staging and change-set environments share its data plane) and one for
production. Valxel can't open a cloud account for anyone: signing up takes a person, a payment method
and the cloud's own terms. So inception asks whether the two exist, and if not, says how to make
them, with links to the cloud's own pages; then each is connected in full mode from the connections
page (deploy/<cloud>-connect), and the product gate builds a data plane in each.

    guide(cloud)            what the person reads: the cloud's sign-up, how to make the two accounts,
                            how to connect them
    parse(cloud, text)      "non-production: acme-cln-dev, production: acme-cln-prod, region: us-central1"
                            -> {"accounts": {...}, "region": ...}, or what's wrong
    runtime_cloud(...)      product.yaml's runtime.cloud from those

Two placements (constitution VX-19 lets either hold a product's environments in the customer's own
accounts):

    shared      the default: the product runs on its workspace's two shared clusters, in the
                workspace's two accounts (set once in the workspace's settings), each product in its
                own namespace with its own cloud identities and network policy. One pair of clusters
                for every product, so one pair of cluster fees.
    dedicated   the product's own two accounts and its own two clusters: for a team that wants the
                isolation and pays for it, and required for products with PHI or payments data
                (DEDICATED_CLASSES).
"""

from __future__ import annotations

import re

NAMES = {"aws": "AWS", "gcp": "Google Cloud", "azure": "Azure"}
NOUN = {"aws": "account", "gcp": "project", "azure": "subscription"}
PLANES = ("non_production", "production")  # the two accounts, and the data plane in each
# Which account each pipeline environment's resources and workloads live in.
ENV_PLANE = {"dev": "non_production", "staging": "non_production", "production": "production"}

ACCOUNT_ID = {
    "aws": re.compile(r"\d{12}"),
    "gcp": re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]"),
    "azure": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
}
EXAMPLE = {
    "aws": ("111111111111", "222222222222", "us-east-1"),
    "gcp": ("acme-cln-nonprod", "acme-cln-prod", "us-central1"),
    "azure": ("1b2c3d4e-0000-4000-8000-000000000001", "1b2c3d4e-0000-4000-8000-000000000002", "eastus2"),
}
REGION = re.compile(r"[a-z]{2,}[a-z0-9-]*[0-9a-z]")

# The cloud's own pages. Valxel links to them; it never signs anyone up.
LINKS = {
    "aws": {
        "sign_up": "https://portal.aws.amazon.com/billing/signup",
        "new_account": "https://console.aws.amazon.com/organizations/v2/home/accounts",
        "billing": "https://console.aws.amazon.com/billing/home",
    },
    "gcp": {
        "sign_up": "https://cloud.google.com/free",
        "new_account": "https://console.cloud.google.com/projectcreate",
        "billing": "https://console.cloud.google.com/billing/projects",
    },
    "azure": {
        "sign_up": "https://azure.microsoft.com/free",
        "new_account": "https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBladeV2",
        "billing": "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/overview",
    },
}

STEPS = {
    "aws": [
        "If your company has no AWS account yet, sign up ({sign_up}): it takes an email, a phone and a card.",
        "In AWS Organizations ({new_account}), add two accounts for {product}: one for non-production and one for "
        "production. Separate accounts keep production's data and permissions apart.",
        "Note each account's 12-digit ID.",
    ],
    "gcp": [
        "If your company has no Google Cloud organization or billing account yet, sign up ({sign_up}).",
        "Create two projects for {product} ({new_account}): one for non-production and one for production. "
        "Give Valxel projects of its own: Google Cloud scopes Valxel's roles to the whole project.",
        "Link both to your billing account ({billing}), and note each project's ID (not its name or number).",
    ],
    "azure": [
        "If your company has no Azure account yet, sign up ({sign_up}).",
        "Create two subscriptions for {product} ({new_account}): one for non-production and one for production. "
        "Azure scopes Valxel's roles to the whole subscription, so give it subscriptions of its own.",
        "Note each subscription's ID (a GUID).",
    ],
}


PLACEMENTS = ("shared", "dedicated")
# Data classes that never share clusters or accounts with other products.
DEDICATED_CLASSES = ("phi", "payments")


def needs_dedicated(data_class: str) -> bool:
    return (data_class or "").lower() in DEDICATED_CLASSES


def shared_plane(workspace: str, kind: str) -> str:
    """The workspace's shared cluster for `kind` (non_production or production): a name GKE, AKS and
    OpenChoreo all accept (lowercase, 31 characters at most)."""
    import hashlib

    slug = re.sub(r"[^a-z0-9-]+", "-", workspace.lower()).strip("-") or "ws"
    if not slug[0].isalpha():
        slug = "w" + slug
    suffix = "-nonprod" if kind == "non_production" else "-prod"
    if len(slug) + len(suffix) > 31:
        slug = slug[: 31 - len(suffix) - 5].rstrip("-") + "-" + hashlib.sha256(workspace.encode()).hexdigest()[:4]
    return slug + suffix


def name(cloud: str) -> str:
    return NAMES.get(cloud, cloud)


def noun(cloud: str) -> str:
    return NOUN.get(cloud, "account")


def guide(cloud: str, product: str = "the product") -> dict:
    """How to get the two accounts ready, as data (the API) and text (the chat)."""
    links = LINKS.get(cloud, {})
    steps = [s.format(product=product, **links) for s in STEPS.get(cloud, [])]
    steps.append(f"Connect each {noun(cloud)} in full mode from Valxel's connections page: it opens "
                 f"{'the AWS console' if cloud == 'aws' else 'Cloud Shell'} with a template that gives Valxel its "
                 "roles there, and nothing else. Valxel builds the product's data plane in each at the product gate.")
    return {"cloud": cloud, "name": name(cloud), "noun": noun(cloud), "links": links, "steps": steps}


def guide_text(cloud: str, product: str = "the product") -> str:
    g = guide(cloud, product)
    lines = [f"Valxel can't open {g['name']} {g['noun']}s for you (they need a person, a payment method and "
             f"{g['name']}'s own terms), but here's what to do:"]
    lines += [f"{i}. {s}" for i, s in enumerate(g["steps"], 1)]
    return "\n".join(lines)


def ask(cloud: str, product: str = "the product") -> str:
    """The inception question."""
    n = noun(cloud)
    return (f"{product} will run on {name(cloud)}, where this Valxel runs. It needs two {n}s of its own: one for "
            f"non-production (dev and staging) and one for production. Do you have them?")


def ask_ids(cloud: str) -> str:
    np, p, region = EXAMPLE.get(cloud, ("", "", ""))
    return (f"Give the two {noun(cloud)} IDs and the region, like: non-production: {np}, production: {p}, "
            f"region: {region}")


def parse(cloud: str, text: str) -> tuple[dict, str]:
    """The two accounts and the region from what the person wrote, or ({}, what's wrong)."""
    pattern = ACCOUNT_ID.get(cloud)
    if pattern is None:
        return {}, f"{cloud} isn't a cloud Valxel builds on"
    t = text.strip()
    labelled = {}
    rest = t
    for key, label in (("non_production", r"non[- ]?prod(?:uction)?|nonprod|dev(?:elopment)?|test"),
                       ("production", r"(?<!non-)(?<!non )(?<!non)prod(?:uction)?"),
                       ("region", r"region|location")):
        m = re.search(rf"\b(?:{label})\b\s*[:=]\s*([A-Za-z0-9-]+)", rest, re.I)
        if m:
            labelled[key] = m.group(1).strip().lower()
            rest = rest[:m.start()] + " " + rest[m.end():]
    accounts = {k: labelled[k] for k in PLANES if k in labelled}
    if not accounts:  # unlabelled: in the order given
        words = re.sub(r"\b(?:non[- ]?production|nonprod|production|prod|and|region|location)\b", " ", rest.lower())
        ids = [m.group(0) for m in pattern.finditer(words)]
        accounts = dict(zip(PLANES, ids, strict=False))
    n = noun(cloud)
    for k in PLANES:
        if not pattern.fullmatch(accounts.get(k, "")):
            what = "non-production" if k == "non_production" else "production"
            return {}, f"I couldn't find the {what} {n} ID. " + ask_ids(cloud)
    if accounts["non_production"] == accounts["production"]:
        return {}, f"Production needs a {n} of its own, apart from non-production. " + ask_ids(cloud)
    region = labelled.get("region", "")
    if not REGION.fullmatch(region):
        return {}, "Which region should it run in? " + ask_ids(cloud)
    return {"accounts": accounts, "region": region}, ""


def runtime_cloud(cloud: str, accounts: dict, region: str, placement: str = "dedicated") -> dict:
    """product.yaml runtime.cloud: each pipeline environment's account (dev and staging share the
    non-production one, where their data plane is), and whether those are the workspace's shared
    accounts or the product's own."""
    return {"provider": cloud, "region": region, "placement": placement,
            "accounts": {env: accounts[plane] for env, plane in ENV_PLANE.items()}}


def ask_placement(cloud: str, product: str, workspace: str) -> str:
    n = noun(cloud)
    return (f"{product} will run on {name(cloud)}, where this Valxel runs. It can share {workspace}'s two clusters "
            f"(the cheapest: one pair of clusters for every product), or have two {n}s and clusters of its own, "
            f"which cost more. A product with PHI or payments data always gets its own. Which?")


def shared_accounts(ws: dict) -> dict:
    """The workspace's shared accounts, when its admin has set them: {accounts, region}."""
    c = (ws or {}).get("cloud_accounts") or {}
    accounts = {k: str(c.get(k) or "") for k in PLANES}
    return {"accounts": accounts, "region": str(c.get("region") or "")} if all(accounts.values()) and c.get("region") \
        else {}


def plane_accounts(cloud: dict) -> dict[str, str]:
    """runtime.cloud's accounts by data plane: {"non_production": ..., "production": ...}."""
    accounts = (cloud or {}).get("accounts") or {}
    return {"non_production": str(accounts.get("staging") or accounts.get("dev") or ""),
            "production": str(accounts.get("production") or "")}
