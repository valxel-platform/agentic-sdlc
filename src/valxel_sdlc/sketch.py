"""A rough sketch at G1 (T-92): the main screens the discovery brief implies, as a grey wireframe
beside it, so the product's people can talk about scope with something to point at before the
spec is written.

    discovery passes -> discovery_sketch: the designer draws one rough wireframe page from the
    brief (artifact SKETCH) -> G1 shows it beside the brief -> recorded with G1's documents

Rough on purpose: boxes, placeholder lines and labels, no colours, no brand, no real copy; the tool
adds a fixed banner saying it's a rough sketch for scope, not the design, and greys the whole page
when it's served or recorded. The design comes at G3 (prototype.py). No script runs: served with a
content policy that allows none, in a sandbox. Skipped when nothing people see changes.
"""

from __future__ import annotations

import html
import re

from .prototype import _FORBIDDEN

KIND = "SKETCH"
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; sandbox"
BANNER = ("<div style=\"position:sticky;top:0;z-index:2147483647;background:#fff;border-bottom:2px dashed #555;"
          "padding:6px 10px;font:13px/1.4 system-ui,sans-serif;color:#222\">Rough sketch, to talk about scope: "
          "not the design. The design comes at G3.</div>")
GREY = "<style>html{filter:grayscale(1)}</style>"

STYLE = """
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;color:#333;background:#fff}
main{max-width:760px;margin:0 auto;padding:20px}.screen{border:2px dashed #888;border-radius:6px;padding:16px;margin:16px 0}
.box{border:1.5px solid #999;border-radius:4px;padding:10px;margin:8px 0;background:#f2f2f2}
.line{height:10px;background:#d6d6d6;border-radius:3px;margin:6px 0}.line.short{width:40%}
.btn{display:inline-block;border:1.5px solid #666;border-radius:4px;padding:4px 12px;margin-right:8px}
h1{font-size:20px}h2{font-size:16px;margin:0 0 8px}
"""


def check(doc: str) -> list[str]:
    out = [f"The sketch: {why}" for pattern, why in _FORBIDDEN if pattern.search(doc or "")]
    if not re.search(r"<html[^>]*\slang\s*=", (doc or "").lower()):
        out.append('The sketch: set the page language (<html lang="en">)')
    if not re.search(r"<h[12]\b", doc or "", re.IGNORECASE):
        out.append("The sketch: name each screen in a heading")
    return out


def assemble(doc: str) -> str:
    """The sketch as served and recorded: greyed, with the banner first."""
    m = re.search(r"<body[^>]*>", doc, re.IGNORECASE)
    if not m:
        return GREY + BANNER + doc
    return doc[:m.end()] + GREY + BANNER + doc[m.end():]


def stub(brief: str, title: str) -> str:
    """A plain sketch from the brief alone (the stand-in designer)."""
    goals = [g.strip("-* ").strip() for g in re.findall(r"^\s*[-*]\s+(.+)$", brief or "", re.MULTILINE)][:3]
    rows = "".join(f'<div class="box">{html.escape(g[:80])}<div class="line short"></div></div>' for g in goals) or \
        '<div class="box"><div class="line"></div><div class="line short"></div></div>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sketch</title><style>{STYLE}</style>'
            f'</head><body><main><h1>{html.escape(title)}</h1><section class="screen"><h2>Main screen</h2>{rows}'
            '<span class="btn">Primary action</span><span class="btn">Cancel</span></section></main></body></html>')


def make(services, state: dict, ctx: dict, brief: str, budget_usd: float | None = None) -> dict:
    """Draw it, check it, keep it. Returns the state update."""
    from .graph.phase import call_agent

    tail = ("Sketch the main screen or two this discovery brief implies, as a rough wireframe the product's people "
            "can point at while they agree the scope: one HTML page (<html lang=\"en\">, inline CSS only, no script, "
            "no event handlers, nothing loaded from elsewhere). Grey boxes, placeholder lines and short labels for "
            "what each area is for; a heading naming each screen. No colours, no brand, no real copy, no example "
            "data: it must look unfinished, because the design comes later. Reply with the HTML document only."
            "\n\n<brief>\n" + brief + "\n</brief>")
    res = call_agent(services, role="designer", phase="discovery", ctx=ctx, skills=(), tail=tail,
                     stub_text=stub(brief, state.get("title", "")), max_tokens=6000, budget_usd=budget_usd)
    text = res.text or ""
    start = max(text.lower().find("<!doctype"), 0) if "<!doctype" in text.lower() else text.lower().find("<html")
    end = text.lower().rfind("</html>")
    doc = text[start:end + len("</html>")] if start >= 0 and end > start else text
    found = check(doc)
    if found:  # one this rough isn't worth a second try: say what's wrong and show the plain one
        doc = stub(brief, state.get("title", ""))
    ref = services.artifacts.save(feature_id=state["feature_id"], kind=KIND, phase="discovery", content=doc,
                                  parents=[], constitution_version=ctx.get("constitution_version", ""))
    return {"artifacts": {KIND: ref}, "cost_usd": float(res.cost_usd or 0.0), "tokens_used": int(res.tokens or 0),
            "sketch": {"version": ref["version"], "findings": found}}


def page(services, state: dict) -> str | None:
    ref = (state.get("artifacts") or {}).get(KIND)
    if not ref:
        return None
    try:
        return assemble(services.artifacts.load(ref))
    except OSError:
        from .drafts import from_record

        return from_record(services, state, KIND)
