"""Undoing a change in production, one feature at a time (T-101 to T-104).

When something released breaks, the smallest undo wins:

    1. turn the feature's flag off, and the flags of the live features that depend on it (dependents first);
    2. roll back the component, when a flag can't help, and only if its data changes allow it;
    3. fix forward with a hotfix, when they don't;
    4. the whole release only when the cause can't be found: never proposed by the tool.

The architecture (G4) says what each feature needs for that, and the tool keeps it with the feature
when G4 approves:

    ## Depends on features     - <FEATURE-ID>: <what it uses>     or  - None: <why>
    ## Flag off                - <what users get with the flag off: today's behavior>
    ## Data ... - Rollback: <safe|not safe>; <what happens to data the new version wrote>
"""

from __future__ import annotations

import re

from .agents.parse import parse_affected_repos, parse_data, parse_new_repos, section

FEATURE_ID = re.compile(r"\b([A-Z][A-Z0-9]*-FEAT-\d+)\b")
_SAFE = re.compile(r"^\s*(safe|not safe|unsafe)\s*(?:[;:,.-]\s*|$)(.*)$", re.IGNORECASE)
ORDER = {"flag_off": 0, "rollback": 1, "hotfix": 2, "intent_change": 3, "monitor": 4}


def parse_depends(hld: str) -> dict:
    """'## Depends on features': the features this one uses, or None."""
    text = section(hld, "Depends on features")
    out: dict = {"present": bool(text.strip()), "ids": [], "bad": []}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if body.lower().startswith("none"):
            continue
        m = FEATURE_ID.match(body)
        if m:
            out["ids"].append(m.group(1))
        else:
            out["bad"].append(line)
    return out


def parse_flag_off(hld: str) -> str:
    """'## Flag off': what users get when the feature's flag is off."""
    lines = [ln.strip()[2:].strip() for ln in section(hld, "Flag off").splitlines() if ln.strip().startswith("- ")]
    return "; ".join(x for x in lines if x)


def rollback_safety(line: str) -> dict:
    """The Data section's Rollback line: whether a rollback is safe, and why. safe None: not said."""
    m = _SAFE.match(line or "")
    if not m:
        return {"safe": None, "why": (line or "").strip()}
    return {"safe": m.group(1).lower() == "safe", "why": m.group(2).strip()}


_VERSION = re.compile(r"^-\s*([\w./-]+)\s*:\s*v?(\d+\.\d+\.\d+)\s*->\s*v?(\d+\.\d+\.\d+)")


def _short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def introduces(hld: str) -> dict:
    """What the design adds that a later feature can build on: new tables, new repos, and the API
    versions it raises a contract to."""
    d = parse_data(hld) if section(hld, "Data") else {"tables": []}
    versions = {}
    for line in section(hld, "Contracts").splitlines():
        if m := _VERSION.match(line.strip()):
            versions[_short(m.group(1))] = m.group(3)
    return {"tables": sorted({t["table"] for t in d["tables"] if t["change"] == "new"}),
            "repos": sorted({_short(r["name"]) for r in parse_new_repos(hld)}), "versions": versions}


def uses(hld: str) -> dict:
    """What the design builds on: the tables it names, the repos it changes, the contract versions it
    starts from."""
    d = parse_data(hld) if section(hld, "Data") else {"tables": []}
    starts = {}
    for line in section(hld, "Contracts").splitlines():
        if m := _VERSION.match(line.strip()):
            starts[_short(m.group(1))] = m.group(2)
    return {"tables": {t["table"] for t in d["tables"]}, "repos": {_short(r["name"]) for r in parse_affected_repos(hld)},
            "starts": starts}


def derived(hld: str, others: list[dict]) -> dict[str, list[str]]:
    """Features this design builds on, worked out from what each introduced (T-105): feature id ->
    what it uses. others: [{feature_id, title, introduces}] of the product's other designed features."""
    u = uses(hld)
    out: dict[str, list[str]] = {}
    for o in others:
        i = o.get("introduces") or {}
        why = [f"table {t}" for t in i.get("tables") or [] if t in u["tables"]]
        why += [f"repo {r}, which it creates" for r in i.get("repos") or [] if r in u["repos"]]
        why += [f"{r} {v}, the API version it introduces" for r, v in (i.get("versions") or {}).items()
                if u["starts"].get(r) == v]
        if why:
            out[o["feature_id"]] = why
    return out


def facts(hld: str) -> dict:
    """What an incident needs to know about the feature, from its approved architecture, and what it
    introduces for later features to build on."""
    d = parse_data(hld) if section(hld, "Data") else {"tables": [], "rollback": ""}
    changed = any(t["change"] != "none" for t in d["tables"])
    rb = rollback_safety(d["rollback"]) if changed else {"safe": True, "why": "no data changes"}
    return {"depends_on": parse_depends(hld)["ids"], "flag_off": parse_flag_off(hld), "rollback": rb,
            "introduces": introduces(hld)}


def _dependents(fid: str, live: dict[str, dict]) -> list[str]:
    """Live features that depend on fid, directly or through others; the furthest first."""
    seen: list[str] = []
    frontier = [fid]
    while frontier:
        nxt = []
        for f in frontier:
            for d, info in live.items():
                if f in (info.get("depends_on") or []) and d not in seen and d != fid:
                    seen.append(d)
                    nxt.append(d)
        frontier = nxt
    return list(reversed(seen))


def _note(a: dict, note: str) -> None:
    a.setdefault("notes", []).append(note)


def _release_note(fid: str, info: dict) -> str:
    rel = info.get("release") or {}
    others = int(rel.get("size") or 0) - 1
    if not rel.get("id") or others <= 0:
        return ""
    return f"{fid} shipped in {rel['id']} with {others} other feature{'s' if others != 1 else ''}: they stay on"


def plan(actions: list[dict], live: dict[str, dict], component: str = "") -> list[dict]:
    """Triage's proposals made safe to carry out: dependents' flags off first, a rollback swapped for
    a hotfix when the data can't go back, everything in the order above. live: feature id -> what
    the tool knows of each feature in production (flag, flag_on, depends_on, rollback, repos, release)."""
    out: list[dict] = []
    off = {a.get("feature_id") for a in actions if a["type"] == "flag_off"}
    fixing = {a.get("repo") for a in actions if a["type"] == "hotfix"}
    for a in actions:
        a = dict(a)
        if a["type"] == "flag_off" and a.get("feature_id"):
            fid = a["feature_id"]
            for d in _dependents(fid, live):
                info = live[d]
                if info.get("flag") and info.get("flag_on") and d not in off:
                    off.add(d)
                    out.append({"type": "flag_off", "feature_id": d, "text": f"{d}: depends on {fid}",
                                "why": f"{d} ({info.get('title', '')}) depends on {fid}: off first"})
                elif not info.get("flag"):
                    _note(a, f"{d} ({info.get('title', '')}) depends on it and has no flag: it may fail while "
                             f"{fid} is off")
            if fid in live and (n := _release_note(fid, live[fid])):
                _note(a, n)
            if fid in live and live[fid].get("flag_off"):
                _note(a, f"With the flag off: {live[fid]['flag_off']}")
        elif a["type"] == "rollback":
            comp = a.get("component") or component
            touched = {f: i for f, i in live.items() if comp and comp in (i.get("repos") or [])}
            unsafe = {f: i for f, i in touched.items() if (i.get("rollback") or {}).get("safe") is False}
            if unsafe:
                why = "; ".join(f"{f}: {(i.get('rollback') or {}).get('why') or 'its data changes can not go back'}"
                                for f, i in unsafe.items())
                if comp in fixing:
                    continue  # a hotfix is already proposed for it
                fixing.add(comp)
                a = {"type": "hotfix", "repo": comp, "change": f"fix forward (rolling back isn't safe: {why})",
                     "text": f"{comp}: fix forward", "why": f"Replaces a rollback of {comp}: {why}"}
            elif touched:
                names = ", ".join(sorted(touched))
                _note(a, f"Rolling back {comp} also takes out: {names}")
        out.append(a)
    out.sort(key=lambda x: ORDER.get(x["type"], 9))
    for n, a in enumerate(out):
        a["id"] = f"A{n + 1}"
        if a.get("notes"):
            a["text"] = (a.get("text") or "").rstrip() + " (" + "; ".join(a["notes"]) + ")"
    return out
