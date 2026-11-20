"""The G3 prototype (T-90): the UX spec's flow as one clickable page, so the product's people
navigate the change as a user would, feel it, and approve it or send the design back.

    the UX spec passes its checks -> the designer draws one HTML document: a section per screen,
    from the product's design record (tokens, components) and the spec's copy, states and flow
    -> checked -> kept with the spec's version (artifact PROTOTYPE) -> at G3 the people open it in a
    sandboxed frame at desktop or phone width, click through the flow, try each state, and see
    what's new -> approve, or request changes (with comments per screen) and it's drawn again

The designer writes no script. It marks up what should happen, and the tool's own fixed runtime
(RUNTIME, added when the page is served or recorded) does it:

    <section data-screen="S1" aria-label="Check-in">     one per screen; the first is where it starts
    <button data-go="S2">Save</button>                   go to S2 (or "done", "cancel"); <a href="#S2"> too
    <div data-state="empty">                             one of the screen's states; a bar switches them
    <span data-bind="visit-reason">                      shows what was typed in the field with that id
    <button data-append="visits" data-template="visit">  adds a copy of <template id="visit"> to the
                                                         list with id "visits", filled from the fields
    <div data-change="new">  /  data-change="changed"    what this feature adds or changes, shown on
                                                         "Show what's new"

Fields keep what was typed as the person moves between screens: it's one page. Nothing leaves it:
served with a content policy that allows only the runtime's own script, no connections, nothing
loaded from elsewhere, and in a sandbox without the app's origin, so it can't read the person's
session or reach the API. Example values only (never anything that looks like a real person's record).

The checks: a section for every screen in the spec; a control for every step of the spec's flow,
on the screen it starts from; the spec's copy word for word on its screen; every state the spec
lists; data-bind, data-append and data-template that point at something; no script, event handler
or outside resource from the designer; and a static accessibility pass (language, labels, words on
buttons, a heading per screen). The full scan runs on the built screens in staging (screens.py).
"""

from __future__ import annotations

import base64
import hashlib
import html
import re

from .agents.parse import (
    parse_copy,
    parse_flow,
    parse_screen_apps,
    parse_screen_kinds,
    parse_screens,
    parse_states,
)

KIND = "PROTOTYPE"
END = ("done", "cancel")

_SECTION = re.compile(r"<section\b[^>]*\bdata-screen\s*=\s*[\"'](S\d+)[\"'][^>]*>(.*?)</section>", re.DOTALL | re.IGNORECASE)
_GO = re.compile(r"""data-go\s*=\s*["']([\w-]+)["']|href\s*=\s*["']#(S\d+)["']""", re.IGNORECASE)
_ATTR = {name: re.compile(rf"""{name}\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
         for name in ("data-bind", "data-append", "data-template", "data-state", "id")}
_FORBIDDEN = (
    (re.compile(r"<script\b", re.IGNORECASE), "no scripts: the tool adds the only one (use data-go, data-state, data-bind)"),
    (re.compile(r"\son[a-z]+\s*=", re.IGNORECASE), "no event handlers (onclick and the like)"),
    (re.compile(r"""(src|href|action|srcset|poster|data)\s*=\s*["']?\s*(https?:|//|javascript:)""", re.IGNORECASE),
     "nothing loaded from elsewhere and no links out: everything inline"),
    (re.compile(r"@import|url\(\s*[\"']?\s*(https?:|//)", re.IGNORECASE), "no outside stylesheets, fonts or images"),
    (re.compile(r"<(iframe|object|embed|base|link|frame)\b|http-equiv|srcdoc", re.IGNORECASE),
     "no frames, embeds, <base>, <link> or meta refresh"),
    (re.compile(r"<form[^>]*\saction\s*=", re.IGNORECASE), "a form must not submit anywhere"),
    (re.compile(r"""class\s*=\s*["'][^"']*\bvx-""", re.IGNORECASE), "the vx- class names are the tool's: use others"),
)


def screens_in(doc: str) -> dict[str, str]:
    """The prototype's screens: {'S1': '<the section's inner HTML>'}."""
    return {m.group(1): m.group(2) for m in _SECTION.finditer(doc or "")}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def check(ux: str, doc: str) -> list[str]:
    """What's wrong with the prototype, against the UX spec; [] when it's ready for G3."""
    screens, copy, states, flow = parse_screens(ux), parse_copy(ux), parse_states(ux), parse_flow(ux)
    found = screens_in(doc)
    low = (doc or "").lower()
    out: list[str] = []
    for pattern, why in _FORBIDDEN:
        if pattern.search(doc or ""):
            out.append(f"The prototype: {why}")
    if not re.search(r"<html[^>]*\slang\s*=", low):
        out.append('The prototype: set the page language (<html lang="en">)')
    out += [f"{sid} ({name}) has no screen in the prototype (<section data-screen=\"{sid}\">)"
            for sid, name in screens.items() if sid not in found]
    out += [f"{sid} is in the prototype but isn't a screen in the UX spec" for sid in found if sid not in screens]
    ids = set(_ATTR["id"].findall(doc or ""))
    for sid, inner in found.items():
        if sid not in screens:
            continue
        if not re.search(r"<h[1-3]\b", inner, re.IGNORECASE):
            out.append(f"{sid}: give the screen a heading")
        text = _text(inner)
        for c in copy.get(sid, []):
            if re.sub(r"\s+", " ", c) not in text:
                out.append(f"{sid}: the copy \"{c}\" from the UX spec isn't on the screen word for word")
        have = {s.lower() for s in _ATTR["data-state"].findall(inner)}
        for st in ("loading", "empty", "error"):
            said = states.get(sid, "")
            if (re.search(rf"\b{st}\b", said, re.IGNORECASE) and not re.search(rf"\bno {st}\b", said, re.IGNORECASE)
                    and st not in have):
                out.append(f"{sid}: show the {st} state (data-state=\"{st}\")")
        out += _a11y(sid, inner)
    for a, b, how in flow:
        if a == "start" or a not in found:
            continue
        targets = {x or y for x, y in _GO.findall(found[a])}
        if b not in targets:
            out.append(f"{a} -> {b}{f' ({how})' if how else ''}: no control on {a} goes there (data-go=\"{b}\")")
    for name in ("data-bind", "data-append", "data-template"):
        for ref in set(_ATTR[name].findall(doc or "")):
            if ref not in ids:
                out.append(f"The prototype: {name}=\"{ref}\" points at nothing (no element with id=\"{ref}\")")
    return out


def _a11y(sid: str, inner: str) -> list[str]:
    out = []
    low = inner.lower()
    for img in re.findall(r"<img\b[^>]*>", low):
        if " alt=" not in img:
            out.append(f"{sid}: every image needs alt text")
            break
    labelled = set(re.findall(r"<label[^>]*\sfor\s*=\s*[\"']([^\"']+)", low))
    for field in re.findall(r"<(?:input|select|textarea)\b[^>]*>", low):
        if re.search(r"type\s*=\s*[\"']?(hidden|submit|button)", field):
            continue
        fid = re.search(r"\sid\s*=\s*[\"']([^\"']+)", field)
        if not (fid and fid.group(1) in labelled) and "aria-label" not in field:
            out.append(f"{sid}: every field needs a label (<label for> or aria-label)")
            break
    for btn in re.findall(r"<button\b[^>]*>(.*?)</button>", inner, re.DOTALL | re.IGNORECASE):
        if not _text(btn).strip():
            out.append(f"{sid}: every button needs words")
            break
    return out


# ------------------------------------------------------------------------------ the tool's runtime

RUNTIME_CSS = """
.vx-bar{position:sticky;top:0;z-index:2147483647;display:flex;flex-wrap:wrap;gap:6px;align-items:center;
padding:6px 10px;background:#24292f;color:#fff;font:13px/1.4 system-ui,sans-serif}
.vx-bar button{font:inherit;color:#fff;background:transparent;border:1px solid #6e7781;border-radius:4px;padding:2px 8px;cursor:pointer}
.vx-bar button[aria-pressed=true]{background:#fff;color:#24292f}.vx-bar .vx-where{font-weight:600;margin-right:6px}
.vx-bar .vx-sep{flex:1}.vx-end{padding:32px 20px;text-align:center;font:16px/1.5 system-ui,sans-serif}
html.vx-changes [data-change=new]{outline:3px solid #1a7f37;outline-offset:2px}
html.vx-changes [data-change=changed]{outline:3px dashed #9a6700;outline-offset:2px}
[data-screen][hidden],[data-state][hidden]{display:none!important}
"""

RUNTIME_JS = r"""
(function () {
  var d = document, body = d.body, names = {}, cur = null, changes = false;
  var all = function (sel, root) { return Array.prototype.slice.call((root || d).querySelectorAll(sel)); };
  var screens = all("[data-screen]");
  screens.forEach(function (s) { names[s.getAttribute("data-screen")] = s.getAttribute("aria-label") || ""; });
  var first = screens.length ? screens[0].getAttribute("data-screen") : null;
  var bar = d.createElement("div");
  bar.className = "vx-bar"; bar.setAttribute("role", "toolbar"); bar.setAttribute("aria-label", "Prototype");
  body.insertBefore(bar, body.firstChild);
  var end = d.createElement("section");
  end.className = "vx-end"; end.setAttribute("data-screen", "vx-end"); end.hidden = true;
  body.appendChild(end);
  function bind(root) {
    all("[data-bind]", root).forEach(function (el) {
      var src = d.getElementById(el.getAttribute("data-bind"));
      el.textContent = (src && src.value) || el.getAttribute("data-empty") || "";
    });
  }
  function statesOf(sec) {
    var out = [];
    all("[data-state]", sec).forEach(function (el) {
      var v = el.getAttribute("data-state"); if (out.indexOf(v) < 0) out.push(v);
    });
    return out;
  }
  function setState(sec, st) {
    all("[data-state]", sec).forEach(function (el) { el.hidden = el.getAttribute("data-state") !== st; });
    sec.setAttribute("data-vx-state", st); draw();
  }
  function button(text, pressed, fn) {
    var b = d.createElement("button"); b.type = "button"; b.textContent = text;
    if (pressed !== null) b.setAttribute("aria-pressed", pressed ? "true" : "false");
    b.addEventListener("click", fn); bar.appendChild(b);
  }
  function draw() {
    bar.textContent = "";
    var where = d.createElement("span"); where.className = "vx-where";
    where.textContent = cur && cur !== "vx-end" ? cur + (names[cur] ? ": " + names[cur] : "") : "End of the flow";
    bar.appendChild(where);
    var sec = cur && d.querySelector('[data-screen="' + cur + '"]');
    if (sec) statesOf(sec).forEach(function (st) {
      var label = st.charAt(0).toUpperCase() + st.slice(1);
      button(label, sec.getAttribute("data-vx-state") === st, function () { setState(sec, st); });
    });
    var sep = d.createElement("span"); sep.className = "vx-sep"; bar.appendChild(sep);
    button("Show what's new", changes, function () {
      changes = !changes; d.documentElement.classList.toggle("vx-changes", changes); draw();
    });
    button("Start again", null, function () { go(first); });
  }
  function go(id) {
    if (id === "done" || id === "cancel") {
      end.textContent = "";
      var h = d.createElement("h1");
      h.textContent = id === "done" ? "The flow is done" : "The flow was cancelled";
      var p = d.createElement("p");
      p.textContent = "This is where the person leaves the feature. Use Start again to go through it once more.";
      end.appendChild(h); end.appendChild(p); id = "vx-end";
    }
    var sec = d.querySelector('[data-screen="' + id + '"]');
    if (!sec) return;
    all("[data-screen]").forEach(function (s) { s.hidden = s !== sec; });
    cur = id; bind(d);
    var st = statesOf(sec);
    if (st.length) setState(sec, st.indexOf("success") >= 0 ? "success" : st[0]); else draw();
    window.scrollTo(0, 0);
  }
  function append(btn) {
    var list = d.getElementById(btn.getAttribute("data-append"));
    var tpl = d.getElementById(btn.getAttribute("data-template"));
    if (!list || !tpl || !tpl.content) return;
    var item = tpl.content.cloneNode(true);
    bind(item);
    all("[data-bind]", item).forEach(function (el) { el.removeAttribute("data-bind"); });
    list.appendChild(item);
  }
  d.addEventListener("click", function (e) {
    var t = e.target.closest("[data-go],[data-append],a[href^='#']");
    if (!t) return;
    e.preventDefault();
    if (t.hasAttribute("data-append")) append(t);
    var to = t.getAttribute("data-go") || (t.getAttribute("href") || "").slice(1);
    if (to) go(to);
  });
  d.addEventListener("submit", function (e) { e.preventDefault(); });
  var asked = (location.hash || "").slice(1);
  go(names.hasOwnProperty(asked) ? asked : first);
})();
"""

SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(RUNTIME_JS.encode()).digest()).decode()
CSP = ("default-src 'none'; script-src '" + SCRIPT_HASH + "'; style-src 'unsafe-inline'; img-src data:; "
       "font-src data:; form-action 'none'; base-uri 'none'; frame-ancestors 'self'; sandbox allow-scripts")


def assemble(doc: str) -> str:
    """The designer's document with the tool's runtime: what's served at G3 and recorded."""
    extra = f"<style>{RUNTIME_CSS}</style><script>{RUNTIME_JS}</script>"
    i = doc.lower().rfind("</body>")
    return doc[:i] + extra + doc[i:] if i >= 0 else doc + extra


# ------------------------------------------------------------------------------ drawing it

STYLE = """
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;color:#1b1f1d;background:#f5f6f4}
section[data-screen]{max-width:720px;margin:0 auto;padding:24px 20px}h1{font-size:24px;margin:8px 0 16px}
.card{background:#fff;border:1px solid #d5dbd6;border-radius:8px;padding:16px;margin-bottom:12px}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}.field input{padding:8px;border:1px solid #9aa39c;border-radius:6px;font:inherit}
.btn{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid #1b1f1d;background:#1b1f1d;color:#fff;cursor:pointer}
.btn.secondary{background:#fff;color:#1b1f1d}.note{color:#4d5651;font-size:14px}
.err{border-left:4px solid #b3261e;padding-left:12px}.empty{color:#4d5651;text-align:center;padding:24px}
.skeleton{height:14px;background:#e3e7e4;border-radius:4px;margin:8px 0}
"""


def stub(ux: str) -> str:
    """A plain prototype from the spec alone (the stand-in designer): every screen, state and step."""
    screens, apps, states, copy, flow = (parse_screens(ux), parse_screen_apps(ux), parse_states(ux), parse_copy(ux),
                                         parse_flow(ux))
    parts = []
    for sid, name in screens.items():
        words = copy.get(sid, [])
        heading = words[0] if words else name
        texts = [c for c in words[1:] if len(c.split()) > 4]
        labels = [c for c in words[1:] if len(c.split()) <= 4]
        steps = [(b, how) for a, b, how in flow if a == sid]
        buttons = []
        for i, label in enumerate(labels):
            to = next((b for b, how in steps if label.lower() in how.lower()), steps[i][0] if i < len(steps) else "")
            go = f' data-go="{to}"' if to else ""
            buttons.append(f'<button class="btn{" secondary" if i else ""}" type="button"{go}>{html.escape(label)}</button>')
        covered = {b for b in re.findall(r'data-go="([\w-]+)"', "".join(buttons))}
        buttons += [f'<button class="btn secondary" type="button" data-go="{b}">{html.escape(how or "Next")}</button>'
                    for b, how in steps if b not in covered]
        keys = [k for k in ("loading", "empty", "error") if k in states.get(sid, "").lower()] + ["success"]
        body = {
            "loading": '<div class="card"><div class="skeleton"></div><div class="skeleton"></div></div>',
            "empty": '<div class="card empty"><p>Nothing here yet.</p></div>',
            "error": f'<div class="card err"><p>{html.escape(texts[0]) if texts else "Something went wrong."}</p></div>',
            "success": (f'<div class="card" data-change="new"><div class="field"><label for="{sid}-f1">Details</label>'
                        f'<input id="{sid}-f1" value="Example value"></div></div>'),
        }
        panels = "".join(f'<div data-state="{k}">{body[k]}</div>' for k in keys)
        where = f'<p class="note">{html.escape(apps[sid])}</p>' if apps.get(sid) else ""
        rest = "".join(f'<p class="note">{html.escape(t)}</p>' for t in texts[1:])
        parts.append(f'<section data-screen="{sid}" aria-label="{html.escape(name)}"><h1>{html.escape(heading)}</h1>'
                     f'{where}{panels}{rest}<p>{" ".join(buttons)}</p></section>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Prototype</title><style>{STYLE}</style></head>'
            f'<body>{"".join(parts)}</body></html>')


def draw(services, ctx: dict, ux: str, findings: list[str] | None = None, budget_usd: float | None = None,
         images: list | tuple = (), today: str = ""):
    """The designer draws the prototype; returns (document, result)."""
    from .graph.phase import call_agent

    tail = (
        "Draw this UX spec as one clickable prototype a product owner can walk through as a user: one self-contained "
        "HTML page (<html lang=\"en\">, inline CSS only), styled from the product's design record (tokens, "
        "components) so it feels like the product. Write no script and no event handlers: mark up what happens and "
        "the tool's runtime does it.\n"
        "- One <section data-screen=\"S1\" aria-label=\"<screen name>\"> per screen, in the order of the Flow, each "
        "starting with a heading. Show the app around the screen (its header and navigation) as the product has them.\n"
        "- For every step of the Flow, a control on the screen it starts from: <button type=\"button\" "
        "data-go=\"S2\"> (data-go=\"done\" or \"cancel\" to leave).\n"
        "- Every state the spec lists, as <div data-state=\"loading|empty|error|success\"> inside the screen.\n"
        "- Fields keep what the person typed across screens. Show it elsewhere with <span data-bind=\"<field id>\">. "
        "To add what they entered to a list, put data-append=\"<list id>\" data-template=\"<template id>\" on the "
        "button and a <template id> with data-bind inside.\n"
        "- Mark what this feature adds with data-change=\"new\" and what it changes with data-change=\"changed\".\n"
        "- The spec's Copy word for word, on its screen. Example values only: never a name, date of birth or number "
        "that could be a real person's. Label every field, put words on every button. No class names starting vx-.\n"
        "Reply with the whole HTML document only." + today + "\n\n<ux-spec>\n" + ux + "\n</ux-spec>")
    if findings:
        tail += "\n\nFix these in the prototype:\n" + "\n".join(f"- {f}" for f in findings)
    res = call_agent(services, role="designer", phase="design", ctx=ctx, skills=("design:design-handoff",), tail=tail,
                     stub_text=stub(ux), max_tokens=24000, budget_usd=budget_usd, images=tuple(images))
    text = res.text or ""
    start = text.lower().find("<!doctype")
    start = start if start >= 0 else text.lower().find("<html")
    end = text.lower().rfind("</html>")
    doc = text[start:end + len("</html>")] if start >= 0 and end > start else text
    return doc, res


def make(services, state: dict, ctx: dict, ux: str, budget_usd: float | None = None, images: list | tuple = (),
         today: str = "") -> dict:
    """Draw, check, redraw once if needed, keep. Returns the state update for the feature: the
    `mockups` entry G3 and G6 read, marked as a prototype."""
    doc, res = draw(services, ctx, ux, budget_usd=budget_usd, images=images, today=today)
    found = check(ux, doc)
    cost, tokens = float(res.cost_usd or 0.0), int(res.tokens or 0)
    if found:
        doc2, res2 = draw(services, ctx, ux, found, budget_usd=budget_usd, images=images, today=today)
        cost, tokens = cost + float(res2.cost_usd or 0.0), tokens + int(res2.tokens or 0)
        found2 = check(ux, doc2)
        if len(found2) < len(found):
            doc, found = doc2, found2
    ref = services.artifacts.save(feature_id=state["feature_id"], kind=KIND, phase="design", content=doc, parents=[],
                                  constitution_version=ctx.get("constitution_version", ""))
    screens, apps, drawn, kinds = parse_screens(ux), parse_screen_apps(ux), screens_in(doc), parse_screen_kinds(ux)
    return {"artifacts": {KIND: ref}, "cost_usd": cost, "tokens_used": tokens,
            "mockups": {"prototype": True,
                        "screens": [{"id": s, "name": n, "app": apps.get(s, ""), "phone": kinds.get(s) == "mobile"}
                                    for s, n in screens.items() if s in drawn],
                        "findings": found, "version": ref["version"]}}


def page(services, state: dict) -> str | None:
    """The prototype as served: the designer's document with the tool's runtime."""
    ref = (state.get("artifacts") or {}).get(KIND)
    if not ref:
        return None
    try:
        return assemble(services.artifacts.load(ref))
    except OSError:
        from .drafts import from_record

        return from_record(services, state, KIND)  # recorded with the runtime already in it
