"""The product architecture's diagrams, published for the product gate the way a feature's HLD
diagram is published for G4 (graph/phase.py publish_diagram).

The architect writes three Mermaid diagrams (inception/formats.py): a context diagram and a
deployment diagram, both flowcharts, and a sequence for each journey. The gate page draws all of
them from the Mermaid source; the two flowcharts are also published to the diagram tool:

- lucid: imported as editable Lucidchart documents, shared view-only within the account.
- drawio: nothing exists to commit to yet (the governance repo is created at setup), so the link
  opens the diagram in the draw.io editor, and setup commits the .drawio files to the new
  governance repo's architecture/diagrams/ folder with the rest of the approved record.

The provider is the product's own (tools.diagrams) for an existing product, and the platform's
choice for new ones (registry.yaml `inception.diagrams`). The brief's data class decides whether
the provider may see the design, as for features. Publishing never fails the plan: the Mermaid
source in the document stays the reviewed record.
"""

from __future__ import annotations

import re

from ..agents.parse import section
from ..integrations.diagrams import DiagramRequest, extract_mermaid, parse_flowchart, to_drawio
from . import Vision, formats

PUBLISHED = "Published diagrams"
# (section, file stem, title) for the flowcharts the diagram tool receives
FLOWCHARTS = (("Context diagram", "context", "context"), ("Deployment diagram", "deployment", "deployment"))
PROVIDER_NAMES = {"lucid": "Lucid", "drawio": "draw.io"}


def config(services, vision: Vision) -> dict | None:
    """The diagram tool for this inception: the product's own, or the platform's for new products."""
    if vision.existing and vision.product_id:
        try:
            return services.snapshot(vision.product_id).product.tools.get("diagrams")
        except Exception:
            return None
    return (services.settings.inception or {}).get("diagrams")


def strip_published(text: str) -> str:
    """The document without its links from an earlier version: each publish writes them afresh."""
    return re.sub(rf"(?ms)^## {PUBLISHED}\s*$.*?(?=^## |\Z)", "", text).rstrip() + "\n"


def _with_links(text: str, lines: list[str]) -> str:
    return strip_published(text).rstrip() + f"\n\n## {PUBLISHED}\n" + "\n".join(lines) + "\n"


def publish(services, vision: Vision, brief: str, text: str, version: int) -> tuple[str, list[dict], list[dict]]:
    """Publish the architecture's flowcharts. Returns the document with a Published diagrams section,
    the diagram records (one per flowchart) and events for the thread."""
    from .graph import event, now

    cfg = config(services, vision)
    if not cfg:
        return strip_published(text), [], []
    provider = cfg["provider"]
    name = PROVIDER_NAMES.get(provider, provider)
    dc = formats.data_class(brief)[0] or "phi"
    if not services.settings.tools.allowed(dc, provider):
        note = (f"Not published to {name}: {dc} designs may not be sent to {name}. The diagrams above are the "
                "record.")
        return _with_links(text, [f"- {note}"]), [], [event("architecture", "diagram_withheld", note)]
    publisher = services.diagrams.get(provider)
    pid = formats.product(brief)["id"] or vision.product_id or "PRODUCT"
    links, refs, events = [], [], []
    for heading, stem, label in FLOWCHARTS:
        source = extract_mermaid(section(text, heading))
        if not source:
            continue
        req = DiagramRequest(feature_id=f"{pid}-{stem}", title=f"{vision.name}: {label} v{version}", mermaid=source,
                             repo="", branch="", version=version, settings=cfg)
        try:
            if publisher is None:
                raise RuntimeError(f"no {provider} publisher is configured")
            ref = publisher.publish(req)
        except Exception as exc:  # the gate still reviews the source; the next version retries
            note = f"Publishing the {label} diagram to {name} failed ({type(exc).__name__}: {exc})."
            links.append(f"- {note} The diagram above is the record.")
            events.append(event("architecture", "diagram_failed", note))
            continue
        ref = {**ref, "diagram": stem, "at": now()}
        refs.append(ref)
        links.append(f"- [{label.capitalize()} v{version} in {name}]({ref['url']})")
        events.append(event("architecture", "diagram_published", ref["url"], provider=provider, version=version,
                            diagram=stem))
    if provider == "drawio" and refs:
        links.append("- The .drawio files are committed to the governance repo's architecture/diagrams/ at setup")
    return (_with_links(text, links) if links else strip_published(text)), refs, events


def drawio_files(services, vision: Vision, architecture: str) -> dict[str, str]:
    """For a draw.io product: the flowcharts as .drawio files for the governance repo."""
    cfg = config(services, vision)
    if not cfg or cfg.get("provider") != "drawio":
        return {}
    out = {}
    for heading, stem, label in FLOWCHARTS:
        fc = parse_flowchart(extract_mermaid(section(architecture, heading)))
        if fc.nodes:
            out[f"architecture/diagrams/{stem}.drawio"] = to_drawio(fc, f"{vision.name}: {label}")
    return out
