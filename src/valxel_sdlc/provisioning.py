"""New repositories on the golden path.

The architect proposes a repo in the HLD's `## New repos` section, with an ADR saying why an
existing repo will not do. The evaluator checks the proposal against config/repos.yaml; G4
approves it with the rest of the architecture. After G3 and G4, the provision step creates each
repo in the product's org and applies the standards, so the rest of the pipeline works on it:

  repo from the role's template -> the component's name in the files the template lists
  (.valxel/identity) -> merge settings -> security features (dependency graph, Dependabot,
  secret scanning with push protection, CodeQL default setup) -> CODEOWNERS -> team access from
  the gate approvers -> topics and custom properties -> branch ruleset with required reviews and
  checks -> OpenChoreo component -> a governance PR registering the repo.

Every step is safe to repeat. Until the governance PR merges, the feature carries the new repos
itself (Services.feature_snapshot), so its change set, risk tier and delivery include them.
"""

from __future__ import annotations

import re

# The rules (standards, naming, the proposal check, the plan) are the core's: repo_policy.py.
from .repo_policy import (  # noqa: F401  (re-exported: delivery reads them from here too)
    PERMISSIONS,
    RepoStandards,
    check_proposal,
    component_name,
    effective_standards,
    load_standards,
    naming_policy,
    plan,
    prefix_of,
)

# A template lists, one path per line, the files that carry its own name (the Workload's name, the
# app's name, the component its error reports are tagged with). A new repo gets its component's name
# in each, so its Workload, logs, traces and error reports name it, and its alerts and errors are
# routed to it (ops/signals.py resolve_product).
IDENTITY_FILE = ".valxel/identity"


def give_identity(gh, repo: str, template: str, component: str, branch: str, feature_id: str) -> str:
    """Replace the template's name with the component's in the files the template lists."""
    old = template.split("/", 1)[-1]
    listed = gh.get_file(repo=repo, path=IDENTITY_FILE, ref="")
    if listed is None:
        return f"{template} lists no files to name"
    changed = []
    for path in [ln.strip() for ln in listed.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]:
        text = gh.get_file(repo=repo, path=path, ref="")
        if text is None or old not in text:
            continue
        gh.put_file(repo=repo, branch=branch, path=path, content=text.replace(old, component),
                    message=f"{feature_id}: name the component {component}")
        changed.append(path)
    return f"named {component} in {', '.join(changed)}" if changed else f"no file names {old}"


# Files that belong to the template, not to the repos made from it: the templates are MIT licensed
# by Valxel, and a product's code is its owner's, under whatever terms they choose.
TEMPLATE_ONLY_FILES = ("LICENSE",)


def drop_template_files(gh, repo: str, branch: str, feature_id: str) -> str:
    """Remove the template's own license from a new repo."""
    gone = [path for path in TEMPLATE_ONLY_FILES
            if gh.delete_file(repo=repo, branch=branch, path=path,
                              message=f"{feature_id}: the template's license is not the product's")]
    return f"removed {', '.join(gone)}" if gone else "no template license to remove"


def add_repo_to_manifest(text: str, repo: str, role: str) -> str:
    """Append a repo to product.yaml's `repos:` list, keeping the rest of the file as written."""
    if f"name: {repo}" in text:
        return text
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if re.match(r"^repos:\s*$", ln))
    except StopIteration:
        return text.rstrip() + f"\nrepos:\n- name: {repo}\n  role: {role}\n"
    end = start + 1
    while end < len(lines) and (lines[end].startswith(("-", " ")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    indent = re.match(r"^(\s*)-", lines[start + 1]).group(1) if start + 1 < len(lines) and \
        re.match(r"^(\s*)-", lines[start + 1]) else ""
    lines[end:end] = [f"{indent}- name: {repo}", f"{indent}  role: {role}"]
    return "\n".join(lines) + "\n"


class RepoProvisioner:
    """Applies a plan through the GitHub and OpenChoreo adapters. Returns per-step results; a
    failed step stops that repo, and a rerun picks up where it stopped."""

    def __init__(self, github, openchoreo) -> None:
        self.github = github
        self.openchoreo = openchoreo

    def provision(self, product, p: dict, *, feature_id: str, governance_repo: str, manifest_text: str) -> dict:
        steps: list[dict] = []
        repo, org = p["repo"], p["repo"].split("/", 1)[0]

        def step(name: str, fn):
            try:
                res = fn()
                steps.append({"step": name, "ok": True, **({"detail": res} if isinstance(res, str) else {})})
                return True
            except Exception as exc:
                steps.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return False

        gh = self.github
        ok = step("create", lambda: gh.create_repo(repo=repo, template=p["template"], private=p["visibility"] != "public",
                                                   description=p["purpose"]))
        if ok and p.get("template"):
            ok = step("identity", lambda: give_identity(gh, repo, p["template"], component_name(product, repo),
                                                        p["default_branch"], feature_id))
            ok = ok and step("template_files", lambda: drop_template_files(gh, repo, p["default_branch"], feature_id))
        ok = ok and step("settings", lambda: gh.update_repo(repo=repo, merge=p["merge"], default_branch=p["default_branch"],
                                                            description=p["purpose"], features=p.get("features")))
        ok = ok and step("security", lambda: gh.enable_security(repo=repo, **p["security"]))
        ok = ok and step("codeowners", lambda: gh.put_file(repo=repo, branch=p["default_branch"], path=".github/CODEOWNERS",
                                                           content=p["codeowners"],
                                                           message=f"{feature_id}: CODEOWNERS from governance"))
        ok = ok and step("teams", lambda: [gh.grant_team(org=org, team=t, repo=repo, permission=perm)
                                           for t, perm in sorted(p["teams"].items())])
        ok = ok and step("labels", lambda: (gh.set_topics(repo=repo, topics=p["topics"]),
                                            gh.ensure_org_properties(org=org, names=sorted(p["properties"])),
                                            gh.set_properties(repo=repo, properties=p["properties"])))
        ok = ok and step("ruleset", lambda: gh.upsert_ruleset(repo=repo, branch=p["default_branch"], spec=p["ruleset"]))
        if ok and p.get("tag_ruleset"):
            ok = step("tags", lambda: gh.upsert_ruleset(repo=repo, branch=p["default_branch"], spec=p["tag_ruleset"],
                                                        target="tag"))
        oc = p.get("openchoreo")
        if ok and oc and product.runtime:
            rt = product.runtime
            ok = step("openchoreo", lambda: self.openchoreo.register_component(
                component=(rt.get("components") or {}).get(repo, repo), project=rt.get("openchoreo_project"),
                namespace=rt.get("namespace"), component_type=oc["component_type"], workflow=oc.get("workflow"),
                repo_url=f"https://github.com/{repo}", branch=p["default_branch"],
                parameters=component_parameters(oc, product.data_class, p["role"])))
        pr = None
        if ok:
            def governance_pr():
                nonlocal pr
                path = "product.yaml"
                branch = f"valxel/{feature_id}-repos"
                staged = gh.get_file(repo=governance_repo, path=path, ref=branch) or manifest_text
                updated = add_repo_to_manifest(staged, repo, p["role"])
                if updated != staged:
                    gh.put_file(repo=governance_repo, branch=branch, path=path, content=updated,
                                message=f"{feature_id}: register {repo}")
                pr = gh.open_pr(repo=governance_repo, branch=branch, title=f"{feature_id}: register new repositories",
                                body=f"Created by the Valxel SDLC after G4 approved {feature_id}.\n\n"
                                     f"- {repo} ({p['role']}): {p['purpose']}",
                                files=[path], labels=["valxel", "governance"])
                return pr["url"]
            ok = step("governance", governance_pr)
        return {"repo": repo, "role": p["role"], "purpose": p["purpose"], "ok": ok, "steps": steps,
                "url": f"https://github.com/{repo}", "governance_pr": pr["url"] if pr else None,
                "required_checks": p["ruleset"]["required_checks"],
                "required_approvals": p["ruleset"].get("required_approvals"),
                "status": "provisioned" if ok else "failed"}


def component_parameters(oc: dict, data_class: str, role: str = "") -> dict:
    """The component type's parameters the role sets: the repo's role, which its pods carry as
    `valxel.io/role` for the network policies (platform/openchoreo/component-types.yaml); and for an
    agent, how isolated its pods run (repos.yaml `isolation` per data class: gvisor for PHI and
    payments, standard otherwise)."""
    iso = oc.get("isolation")
    return {**({"role": role} if role else {}),
            **({"isolation": iso.get(data_class, "standard")} if isinstance(iso, dict) else {})}
