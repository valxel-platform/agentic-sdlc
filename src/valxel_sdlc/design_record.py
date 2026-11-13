"""The product's design record: what its apps look like and are made of, kept in the governance repo
next to the architecture record, so the designer designs with the product, not generic advice.

    design/tokens.md       colours, type and spacing, as the product's apps define them
    design/components.md   the components the apps already have: '- <Name>: <what it's for> (<repo>)'
    design/screens.md      the screens people use: '- <id>: <name> (<web|mobile>: <repo>) [<feature>]'
    design/patterns.md     how this product does forms, errors, empty states and navigation

Where it comes from:
    a new product    inception writes a first version (starter tokens and patterns, no components yet)
    an existing one  `valxel design-record --product X` drafts it from the app repos, as a governance PR
    every release    operate proposes the feature's screens (and any new components) to screens.md and
                     components.md, in the same way it proposes decisions to the architecture record

The designer reads it (a knowledge source, valxel_sdlc.knowledge); the UX spec's '## Components'
lists what each design uses, existing or new, and the checks hold it to the record.
"""

from __future__ import annotations

import re

FILES = ("tokens", "components", "screens", "patterns")
_ITEM = re.compile(r"^-[ \t]*([^:\n]+?)[ \t]*:[ \t]*(.+?)[ \t]*$", re.M)
_USES = re.compile(r"^-[ \t]*([^:\n]+?)[ \t]*:[ \t]*(existing|new)\b[ \t]*:?[ \t]*(.*)$", re.M | re.I)


def items(text: str) -> dict[str, str]:
    """'- Name: what' lines -> {name: what} (a 'None' line is no item)."""
    return {n.strip(): w for n, w in _ITEM.findall(text or "") if n.strip().lower() not in ("none", "")}


def uses(ux: str) -> list[dict]:
    """The UX spec's '## Components': '- DataTable: existing', '- ReadyBadge: new: no badge shows status yet'."""
    from .agents.parse import section

    return [{"name": n.strip(), "kind": k.lower(), "why": w.strip()} for n, k, w in _USES.findall(section(ux, "Components"))]


def check(ux: str, record: dict) -> list[str]:
    """A design uses the product's components, or says why it needs a new one."""
    from .agents.parse import parse_screens, section

    if not parse_screens(ux):
        return []
    known = {k.lower() for k in (record or {}).get("components", {})}
    if not section(ux, "Components"):
        return ["No Components section: each component the screens use, as '- <Name>: existing' (from the design "
                "record) or '- <Name>: new: <why no existing one fits>'"] if known or record.get("present") else []
    out = []
    for u in uses(ux):
        if u["kind"] == "existing" and known and u["name"].lower() not in known:
            out.append(f"Component {u['name']} is marked existing but isn't in design/components.md: use one that is, "
                       "or mark it new and say why")
        if u["kind"] == "new" and len(u["why"].split()) < 3:
            out.append(f"Component {u['name']} is new: say why no existing component fits")
    return out


# ------------------------------------------------------------------------------ the knowledge source

class GovernanceDesign:
    """design/*.md in the product's governance repo, at its current head."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, snap, cfg: dict) -> dict:
        src = self.services.governance.source
        gov = self.services.settings.entry(snap.product.product_id).governance
        sha = src.resolve(gov.org, gov.repo, gov.ref)
        base = (cfg.get("design_dir") or "design").strip("/")
        files = {f: src.read(gov.org, gov.repo, f"{base}/{f}.md", sha, optional=True) or "" for f in FILES}
        return {"repo": gov.full_name, "sha": sha, "present": any(files.values()), "files": files,
                "components": items(files["components"]), "screens": items(files["screens"])}


def render(pack: dict, max_chars: int = 5000) -> str:
    """The record as a prompt section for the designer (and the coders of web and mobile tasks)."""
    if not pack or not pack.get("present"):
        note = ("\n## Design record\nThis product has no design record yet (design/ in its governance repo): follow "
                "its apps as they are, and list every component you use as new.")
        return note if len(note) <= max_chars else ""
    f = pack["files"]
    out = [f"\n## Design record ({pack['repo']}@{str(pack.get('sha', ''))[:7]}): design with these, don't reinvent them"]
    for name, title in (("tokens", "Tokens"), ("components", "Components"), ("patterns", "Patterns"),
                        ("screens", "Screens people use today")):
        body = re.sub(r"^#.*$", "", f.get(name, ""), flags=re.M).strip()
        if body:
            out.append(f"### {title}\n{body}")
    text = "\n".join(out)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 40)].rstrip() + "\n[... truncated]" if max_chars > 80 else ""


# ------------------------------------------------------------------------------ writing it

STARTER = {
    "tokens": """# Design tokens

Starter values: replace them with the product's own when its apps define them.

- Colour, text: near-black on white, at least 4.5:1 contrast (WCAG 2.1 AA)
- Colour, action: one accent colour for primary buttons and links
- Colour, status: green for done, amber for waiting, red for errors, never colour alone
- Type: one sans-serif family; body 16 px, small 14 px, headings 21 and 28 px
- Spacing: steps of 4 px (4, 8, 12, 16, 24, 32)
- Corners: 6 px on controls and cards
""",
    "components": """# Components

- None yet: the first features add them, and each release proposes what it added here
""",
    "screens": """# Screens

- None yet: each release proposes the screens it added or changed here
""",
    "patterns": """# Patterns

- Forms: one column, labels above fields, the primary action last; errors beside the field, the form keeps what was typed
- Errors: say what happened and what to do next; a retry where it can help; never a raw error code alone
- Empty states: say what will appear here and offer the first action
- Loading: keep the layout steady; show progress after half a second
- Navigation: the same top-level places on every screen; the current one marked
- Phones: one column, touch targets at least 44 px, the primary action within thumb reach
""",
}


def starter_files(base: str = "design") -> dict[str, str]:
    return {f"{base}/{name}.md": text for name, text in STARTER.items()}


def with_screens(record: str, feature_id: str, ux: str) -> str:
    """screens.md with this feature's screens added (its own lines replaced if it ran before)."""
    from .agents.parse import parse_screen_apps, parse_screens

    names, apps = parse_screens(ux), parse_screen_apps(ux)
    keep = [ln for ln in (record or "# Screens\n").rstrip().splitlines()
            if f"[{feature_id}]" not in ln and not ln.strip().lower().startswith("- none yet")]
    for sid, name in names.items():
        app = apps.get(sid) or ""
        where = f" ({'mobile' if 'mobile' in app else 'web'}: {app})" if app else ""
        keep.append(f"- {feature_id}-{sid}: {name}{where} [{feature_id}]")
    return "\n".join(keep) + "\n"


def with_components(record: str, feature_id: str, ux: str) -> str:
    """components.md with the components this feature added."""
    new = [u for u in uses(ux) if u["kind"] == "new"]
    if not new:
        return record
    lines = [ln for ln in (record or "# Components\n").rstrip().splitlines() if not ln.strip().lower().startswith("- none yet")]
    have = set(items(record).keys())
    lines += [f"- {u['name']}: {u['why'] or 'added'} [{feature_id}]" for u in new if u["name"] not in have]
    return "\n".join(lines) + "\n"


def propose_after_release(services, state: dict, ux: str) -> dict | None:
    """After a release: the feature's screens and new components, proposed to the design record."""
    cfg = services.knowledge.cfg.settings if getattr(services, "knowledge", None) else {}
    base = (cfg.get("design_dir") or "design").strip("/")
    gov = services.settings.entry(state["product_id"]).governance
    src = services.governance.source
    sha = src.resolve(gov.org, gov.repo, gov.ref)
    screens = src.read(gov.org, gov.repo, f"{base}/screens.md", sha, optional=True) or ""
    comps = src.read(gov.org, gov.repo, f"{base}/components.md", sha, optional=True) or ""
    files = {}
    new_screens = with_screens(screens, state["feature_id"], ux)
    if new_screens.strip() != screens.strip():
        files[f"{base}/screens.md"] = new_screens
    new_comps = with_components(comps, state["feature_id"], ux)
    if new_comps.strip() != comps.strip():
        files[f"{base}/components.md"] = new_comps
    if not files:
        return None
    from .roadmap import governance_pr

    pr = governance_pr(services, state["product_id"], branch=f"valxel/{state['feature_id']}-design", files=files,
                       title=f"{state['feature_id']}: record its screens in the design record",
                       body=(f"Released {state['feature_id']} ({state['title']}). This adds its screens"
                             + (" and the components it introduced" if f"{base}/components.md" in files else "")
                             + " to the design record, so future designs build on them.\n\nReview, then merge."),
                       labels=["valxel", "design"])
    return pr


# ------------------------------------------------------------------------------ an existing product

_FILE = re.compile(r"^=== (tokens|components|screens|patterns) ===\s*$", re.M)


def split_files(text: str) -> dict[str, str]:
    """The agent's reply: four files, each after a '=== name ===' line."""
    parts = _FILE.split(text or "")
    return {parts[i]: parts[i + 1].strip() + "\n" for i in range(1, len(parts) - 1, 2)}


def draft_for_existing(services, product_id: str) -> dict:
    """Read the product's web and mobile apps and draft its design record, as a governance pull
    request people review. Returns {"files", "read", "pr"}."""
    from . import workspace
    from .agents.context import build_layers, skills_dir_of
    from .agents.parse import SCREEN_ROLES
    from .gateway import CallTags
    from .roadmap import governance_pr
    from .runtime import AgentTask

    snap = services.snapshot(product_id)
    apps = [r.name for r in snap.product.repos if r.role in SCREEN_ROLES]
    co = workspace.checkout(services, apps, key=f"{product_id}-design-record")
    stub = "\n".join(f"=== {name} ===\n{text}" for name, text in STARTER.items())
    if apps:
        stub = stub.replace("- None yet: the first features add them, and each release proposes what it added here",
                            "\n".join(f"- None found yet in {a}: list its components here" for a in apps))
    tail = ("Draft this product's design record from its apps" + (f" ({', '.join(apps)})" if apps else "")
            + ": the tokens they define (colours, type, spacing), the components they have (one line each: "
              "'- <Name>: <what it's for> (<repo>)'), the screens people use ('- <id>: <name> (<web|mobile>: <repo>) "
              "[<route>]'), and the patterns they follow for forms, errors, empty states, loading and navigation. "
              "Only what you find in the code; say 'not found' rather than guess. Reply with four files, each after "
              "a line '=== tokens ===', '=== components ===', '=== screens ===', '=== patterns ===', each starting "
              "with a '# ' title.")
    if co:
        tail = workspace.describe(co).split(". Under Agent notes")[0] + ".\n\n" + tail
    layers = build_layers(role="designer", skills=("design:design-system",), constitution=snap.constitution,
                          product=snap.product, parent_texts={}, tail=tail, product_context=snap.product_context,
                          skills_dir=skills_dir_of(services))
    tags = CallTags(product_id=product_id, feature_id=f"{product_id}-design-record", phase="design", role="designer",
                    data_class=snap.product.data_class, workspace=snap.product.workspace, prompt_version="design-record@1")
    try:
        res = services.runtime_for("designer").run(AgentTask(
            role="designer", phase="design", tags=tags, layers=layers, skills=("design:design-system",),
            stub_text=stub, max_tokens=6000, cwd=(co or {}).get("root"), read_only=bool(co)))
    finally:
        workspace.release(co)
    files = split_files(res.text) or split_files(stub)
    base = (services.knowledge.cfg.settings.get("design_dir") if getattr(services, "knowledge", None) else None) or "design"
    paths = {f"{base}/{k}.md": v for k, v in files.items() if k in FILES}
    read = {r: v.get("sha", v.get("skipped", "")) for r, v in ((co or {}).get("repos") or {}).items()}
    pr = governance_pr(services, product_id, branch="valxel/design-record", files=paths,
                       title=f"{product_id}: design record (draft from its apps)",
                       body=("A first design record, drafted from the product's apps"
                             + (": " + ", ".join(f"{r} @ {s[:7]}" for r, s in read.items()) if read else
                                " (the code couldn't be read here, so it's a starter to fill in)")
                             + ".\n\nCorrect anything that's wrong, then merge: the designer works from it."),
                       labels=["valxel", "design"])
    return {"files": sorted(paths), "read": read, "pr": pr}
