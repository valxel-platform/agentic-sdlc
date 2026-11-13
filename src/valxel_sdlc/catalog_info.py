"""catalog-info.yaml: Git is the source for the Backstage catalog in the OpenChoreo portal.

Each repo a feature changes carries a Backstage Component entity in `catalog-info.yaml` at its
root. The build commits it to the feature branch next to the code, so it is reviewed with the pull
request, merges with it, and Backstage discovers it from the default branch. Nothing writes to
Backstage directly.

The pipeline fills only the fields it knows and leaves the rest alone: a description, links or
dependencies people add by hand stay. Each feature adds its capability to the
`valxel.io/capabilities` annotation, so the portal shows which capabilities a component carries
and the tool's catalog (`integrations/catalog.py`, Postgres) shows the in-flight ones too.
"""

from __future__ import annotations

import yaml

PATH = "catalog-info.yaml"
CAPABILITIES = "valxel.io/capabilities"

# Backstage component types by repo role (free strings in Backstage; these are its conventions).
TYPES = {"provider": "service", "bff": "service", "event-worker": "service", "mcp-server": "service",
         "agent": "service", "consumer": "website", "mobile": "mobile-app", "library": "library",
         "acceptance": "test-suite"}


def owner_of(product) -> str:
    """The product owners' team as a Backstage group: the first G2 approver that is a team."""
    for who in (product.approvers or {}).get("g2") or []:
        if "/" in who:
            return f"group:{who.split('/', 1)[1]}"
    return f"group:{product.product_id.lower()}"


def _docs(text: str | None) -> list:
    if not text or not text.strip():
        return []
    try:
        return [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return []  # unreadable: replaced with a valid file (the PR shows the change)


def render(existing: str | None, *, repo: str, role: str, product_id: str, owner: str, component: str,
           project: str | None, capability: str) -> str | None:
    """The file with this repo's Component updated, or None when nothing would change."""
    docs = _docs(existing)
    name = component.rsplit("/", 1)[-1]
    comp = next((d for d in docs if isinstance(d, dict) and d.get("kind") == "Component"
                 and ((d.get("metadata") or {}).get("annotations") or {}).get("github.com/project-slug") == repo), None)
    comp = comp or next((d for d in docs if isinstance(d, dict) and d.get("kind") == "Component"
                         and (d.get("metadata") or {}).get("name") == name), None)
    if comp is None:
        comp = {"apiVersion": "backstage.io/v1alpha1", "kind": "Component", "metadata": {"name": name}, "spec": {}}
        docs.append(comp)
    before = yaml.safe_dump_all(docs, sort_keys=False) if existing else ""
    meta = comp.setdefault("metadata", {})
    meta.setdefault("name", name)
    ann = meta.setdefault("annotations", {})
    ann["github.com/project-slug"] = repo
    ann["valxel.io/product"] = product_id
    if project:
        ann["openchoreo.dev/project"] = project
        ann["openchoreo.dev/component"] = name
    caps = [c.strip() for c in str(ann.get(CAPABILITIES, "")).split(",") if c.strip()]
    if capability and capability not in caps:
        caps.append(capability)
    if caps:
        ann[CAPABILITIES] = ",".join(caps)
    spec = comp.setdefault("spec", {})
    spec.setdefault("type", TYPES.get(role, "service"))
    spec.setdefault("lifecycle", "production")
    spec.setdefault("owner", owner)
    spec.setdefault("system", product_id.lower())
    after = yaml.safe_dump_all(docs, sort_keys=False)
    return None if after == before else after


def write(services, *, repo: str, branch: str, role: str, product, component: str, project: str | None,
          capability: str, feature_id: str) -> bool:
    """Commit the updated catalog-info.yaml to the branch; True when it changed."""
    existing = services.github.get_file(repo=repo, path=PATH, ref=branch)
    text = render(existing, repo=repo, role=role, product_id=product.product_id, owner=owner_of(product),
                  component=component, project=project, capability=capability)
    if text is None:
        return False
    services.github.put_file(repo=repo, branch=branch, path=PATH, content=text,
                             message=f"{feature_id}: catalog-info.yaml for {capability}")
    return True
