"""Mockups at G3: each screen of the UX spec as a low-fidelity HTML page, so the people at G3 judge
something they can see, not only a list.

    after the UX spec passes its checks -> the designer draws one page per screen, from the product's
    design record (tokens, components) and the spec's copy and states -> checked -> kept with the spec's
    version (artifact MOCKUPS) -> shown at G3, each in a sandboxed frame, desktop or phone width

A mockup is one self-contained page: inline CSS only, no scripts (the states switch with radio
buttons and CSS), no links out, no images but inline SVG, example values only (never anything that
looks like a real person's record). The checks:

    every screen has one; its copy from the spec appears in it word for word;
    no script, no external resource, no form that submits anywhere;
    a static accessibility pass: the page has a language, every image has alt text, every field a
    label, every button words, and a heading (the full scan runs on the built screens in staging)
"""

from __future__ import annotations

import html
import json
import re

from .agents.parse import parse_copy, parse_screen_apps, parse_screens, parse_states

KIND = "MOCKUPS"
_SPLIT = re.compile(r"^=== (S\d+) ===\s*$", re.M)


def split(text: str) -> dict[str, str]:
    parts = _SPLIT.split(text or "")
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def problems(sid: str, page: str, copy: list[str]) -> list[str]:
    out = []
    low = page.lower()
    if "<script" in low or re.search(r"\son[a-z]+\s*=", low):
        out.append(f"{sid}: no scripts or event handlers (switch states with radio buttons and CSS)")
    if re.search(r"""(src|href|action)\s*=\s*["']?\s*(https?:|//)""", low) or "@import" in low or "url(http" in low:
        out.append(f"{sid}: no external resources or links out: everything inline")
    if re.search(r"<form[^>]*\saction\s*=", low):
        out.append(f"{sid}: a form must not submit anywhere")
    if not re.search(r"<html[^>]*\slang\s*=", low):
        out.append(f"{sid}: set the page language (<html lang=\"en\">)")
    for img in re.findall(r"<img\b[^>]*>", low):
        if " alt=" not in img:
            out.append(f"{sid}: every image needs alt text")
            break
    ids_labelled = set(re.findall(r"<label[^>]*\sfor\s*=\s*[\"']([^\"']+)", low))
    for field in re.findall(r"<(?:input|select|textarea)\b[^>]*>", low):
        if re.search(r"type\s*=\s*[\"']?(radio|hidden)", field):
            continue  # the state switch, and hidden values
        fid = re.search(r"\sid\s*=\s*[\"']([^\"']+)", field)
        if not (fid and fid.group(1) in ids_labelled) and "aria-label" not in field:
            out.append(f"{sid}: every field needs a label (<label for> or aria-label)")
            break
    for btn in re.findall(r"<button\b[^>]*>(.*?)</button>", page, re.S | re.I):
        if not re.sub(r"<[^>]+>", "", btn).strip():
            out.append(f"{sid}: every button needs words")
            break
    if not re.search(r"<h[1-3]\b", low):
        out.append(f"{sid}: give the screen a heading")
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text)
    for c in copy:
        if re.sub(r"\s+", " ", c) not in text:
            out.append(f"{sid}: the copy \"{c}\" from the UX spec isn't on the mockup word for word")
    return out


def check(ux: str, pages: dict[str, str]) -> list[str]:
    screens, copy = parse_screens(ux), parse_copy(ux)
    out = [f"{sid} ({name}) has no mockup" for sid, name in screens.items() if not pages.get(sid)]
    for sid, page in pages.items():
        if sid not in screens:
            out.append(f"Mockup {sid} isn't a screen in the UX spec")
            continue
        out += problems(sid, page, copy.get(sid, []))
    return out


# ------------------------------------------------------------------------------ drawing them

STYLE = """
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;color:#1b1f1d;background:#f5f6f4}
main{max-width:720px;margin:0 auto;padding:24px 20px}h1{font-size:24px;margin:8px 0 16px}
.states{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;font-size:13px}
.states>label{border:1px solid #b8c0ba;border-radius:6px;padding:4px 10px;background:#fff;cursor:pointer}
.states>input{position:absolute;opacity:0}.state{display:none}
.card{background:#fff;border:1px solid #d5dbd6;border-radius:8px;padding:16px;margin-bottom:12px}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}.field input{padding:8px;border:1px solid #9aa39c;border-radius:6px;font:inherit}
.btn{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid #1b1f1d;background:#1b1f1d;color:#fff}
.btn.secondary{background:#fff;color:#1b1f1d}.note{color:#4d5651;font-size:14px}
.err{border-left:4px solid #b3261e;padding-left:12px}.empty{color:#4d5651;text-align:center;padding:24px}
.skeleton{height:14px;background:#e3e7e4;border-radius:4px;margin:8px 0}
@media(max-width:480px){main{padding:16px 12px}}
"""


def stub_page(sid: str, name: str, states: str, copy: list[str], app: str) -> str:
    """A plain wireframe from the spec alone: heading, the copy as headings and buttons, a panel per state."""
    heading = copy[0] if copy else name
    buttons = [c for c in copy[1:] if len(c.split()) <= 4]
    texts = [c for c in copy[1:] if len(c.split()) > 4]
    keys = [k for k in ("loading", "empty", "error", "success") if k in states.lower()] or ["success"]
    radios = "".join(f'<input type="radio" name="st" id="st-{k}"{" checked" if k == "success" else ""}>'
                     f'<label for="st-{k}">{k.capitalize()}</label>' for k in keys)
    shown = "".join(f"#st-{k}:checked~.panels .{k}{{display:block}}" for k in keys)
    body = {
        "loading": '<div class="card"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>',
        "empty": f'<div class="card empty"><p>Nothing here yet.</p>{_buttons(buttons[:1])}</div>',
        "error": f'<div class="card err"><p>{html.escape(texts[0]) if texts else "Something went wrong."}</p>{_buttons(buttons[:1])}</div>',
        "success": ('<div class="card"><div class="field"><label for="f1">Name</label><input id="f1" value="Example value">'
                    f'</div><div class="field"><label for="f2">Details</label><input id="f2" value="Example detail"></div>'
                    f"{_buttons(buttons)}</div>"),
    }
    panels = "".join(f'<div class="state {k}">{body[k]}</div>' for k in keys)
    rest = "".join(f'<p class="note">{html.escape(t)}</p>' for t in texts[1:])
    where = f'<p class="note">{html.escape(app)}</p>' if app else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(sid)}: {html.escape(name)}</title>'
            f"<style>{STYLE}{shown}</style></head><body><main><h1>{html.escape(heading)}</h1>{where}"
            f'<div class="states" role="group" aria-label="State">{radios}<div class="panels" style="width:100%">'
            f"{panels}</div></div>{rest}</main></body></html>")


def _buttons(labels: list[str]) -> str:
    return "".join(f'<button class="btn{" secondary" if i else ""}" type="button">{html.escape(b)}</button> '
                   for i, b in enumerate(labels))


def stub_pages(ux: str) -> str:
    screens, apps, states, copy = parse_screens(ux), parse_screen_apps(ux), parse_states(ux), parse_copy(ux)
    return "\n".join(f"=== {sid} ===\n" + stub_page(sid, name, states.get(sid, ""), copy.get(sid, []), apps.get(sid, ""))
                     for sid, name in screens.items())


def draw(services, ctx: dict, ux: str, findings: list[str] | None = None, budget_usd: float | None = None):
    """The designer draws the mockups; returns (pages, result)."""
    from .graph.phase import call_agent

    tail = ("Draw each screen of this approved-to-be UX spec as a low-fidelity HTML mockup, using the product's design "
            "record (tokens, components) where it has one. One self-contained page per screen: <html lang=\"en\">, "
            "inline CSS only, no scripts and no event handlers, nothing loaded from outside, inline SVG for icons. "
            "Show every state the spec lists, switched by radio buttons with labels and CSS (:checked ~). Put the "
            "spec's Copy on the page word for word. Example values only: never a name, date of birth or number that "
            "could be a real person's. Label every field, put words on every button, and start with a heading. Reply "
            "with each page after a line '=== S1 ===' (the screen's id).\n\n<ux-spec>\n" + ux + "\n</ux-spec>")
    if findings:
        tail += "\n\nFix these in the mockups:\n" + "\n".join(f"- {f}" for f in findings)
    res = call_agent(services, role="designer", phase="design", ctx=ctx, skills=("design:design-handoff",), tail=tail,
                     stub_text=stub_pages(ux), max_tokens=16000, budget_usd=budget_usd)
    return split(res.text), res


def make(services, state: dict, ctx: dict, ux: str, budget_usd: float | None = None) -> dict:
    """Draw, check, redraw once if needed, keep. Returns the state update for the feature."""
    pages, res = draw(services, ctx, ux, budget_usd=budget_usd)
    found = check(ux, pages)
    cost, tokens = float(res.cost_usd or 0.0), int(res.tokens or 0)
    if found:
        pages2, res2 = draw(services, ctx, ux, found, budget_usd=budget_usd)
        cost, tokens = cost + float(res2.cost_usd or 0.0), tokens + int(res2.tokens or 0)
        if len(check(ux, pages2)) < len(found):
            pages = pages2
        found = check(ux, pages)
    ref = services.artifacts.save(feature_id=state["feature_id"], kind=KIND, phase="design",
                                  content=json.dumps(pages), parents=[], constitution_version=ctx.get("constitution_version", ""))
    screens, apps = parse_screens(ux), parse_screen_apps(ux)
    return {"artifacts": {KIND: ref}, "cost_usd": cost, "tokens_used": tokens,
            "mockups": {"screens": [{"id": s, "name": n, "app": apps.get(s, ""), "phone": "mobile" in apps.get(s, "")}
                                    for s, n in screens.items() if s in pages],
                        "findings": found, "version": ref["version"]}}


def page(services, state: dict, sid: str) -> str | None:
    ref = (state.get("artifacts") or {}).get(KIND)
    if not ref:
        return None
    try:
        return json.loads(services.artifacts.load(ref)).get(sid)
    except (ValueError, OSError):
        return None
