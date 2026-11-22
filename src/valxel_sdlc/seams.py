"""The tool's seams, and a check that keeps them from tangling further (ADR 0005).

The tool is one codebase, deployed as an API and workers (run_queue.py). Inside it are four parts
that could one day run on their own, and a shared core:

  intake     turning a request into a feature, an initiative or a new product
  roadmap    initiatives, goals and their outcomes
  delivery   a feature's phases, gates, repos, builds and releases
  ops        what runs: signals, incidents, remediation, costs, upkeep
  core       what they all use: config, governance, agents, the model gateway, storage, tenancy

and a composition root (the engine, the API, the CLI) that wires them together and may use any.

The rules: a seam uses the core and itself; the core uses no seam; a seam doesn't use another
seam. A module that several seams read and that uses no seam itself (a shared definition, a parser,
a record format) belongs to the core, wherever its file sits: config/seams.yaml's `core` says so.
The known breaks burn down to dated targets (`targets`, one per sprint), and the test fails once a
target's date has come and the count is still above it.

config/seams.yaml says which modules make up each seam, and lists every import that breaks the
rules today, as known debt. `tests/test_seams.py` fails on any new one, and on a listed one
that no longer exists (so the list only shrinks). `python -m valxel_sdlc.seams` prints the
current breaks, by pair of seams.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT.parents[1] / "config" / "seams.yaml"


def load(path: Path = CONFIG) -> dict:
    return yaml.safe_load(path.read_text())


def seam_of(module: str, cfg: dict) -> str:
    """The seam a module (dotted, under valxel_sdlc) belongs to: by its longest matching prefix."""
    best, owner = -1, "core"
    for seam, prefixes in {**cfg["seams"], "root": cfg["root"], "core": cfg.get("core") or []}.items():
        for p in prefixes:
            if (module == p or module.startswith(p + ".")) and len(p) > best:
                best, owner = len(p), seam
    return owner


def _module(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports(root: Path = ROOT) -> set[tuple[str, str]]:
    """Every (importer, imported) pair of valxel_sdlc modules, imported names resolved to modules
    where they are modules."""
    known = {_module(p) for p in root.rglob("*.py")}
    out: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        mod = _module(path)
        package = mod.split(".") if path.name == "__init__.py" else mod.split(".")[:-1]
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not isinstance(node, ast.ImportFrom):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith("valxel_sdlc."):
                            out.add((mod, a.name.removeprefix("valxel_sdlc.")))
                continue
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                target = ".".join(base + ([node.module] if node.module else []))
            elif (node.module or "").startswith("valxel_sdlc"):
                target = (node.module or "").removeprefix("valxel_sdlc").lstrip(".")
            else:
                continue
            for a in node.names:
                sub = f"{target}.{a.name}" if target else a.name
                out.add((mod, sub if sub in known else target))
    return {(a, b) for a, b in out if b and a != b}


def breaks(cfg: dict, pairs: set[tuple[str, str]] | None = None) -> set[str]:
    """Imports that cross a seam the wrong way, as 'importer -> imported'."""
    out = set()
    for a, b in pairs if pairs is not None else imports():
        sa, sb = seam_of(a, cfg), seam_of(b, cfg)
        if sa == "root" or sa == sb or sb == "core":
            continue
        out.add(f"{a} -> {b}")  # core into a seam, or seam into another seam (or into the root)
    return out


def over_target(cfg: dict, count: int, today: str) -> dict | None:
    """The burn-down target in force today (config/seams.yaml `targets`) that `count` misses, if any:
    the latest one whose date has come."""
    due = [t for t in cfg.get("targets") or [] if str(t["by"]) <= today]
    if not due:
        return None
    current = max(due, key=lambda t: str(t["by"]))
    return current if count > int(current["at_most"]) else None


def check(cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """(new breaks, listed breaks that are gone)."""
    cfg = cfg or load()
    now, known = breaks(cfg), set(cfg.get("known_breaks") or [])
    return sorted(now - known), sorted(known - now)


def main() -> int:
    cfg = load()
    found = breaks(cfg)
    by_pair = Counter(f"{seam_of(x.split(' -> ')[0], cfg)} -> {seam_of(x.split(' -> ')[1], cfg)}" for x in found)
    for pair, n in sorted(by_pair.items()):
        print(f"{pair:24} {n}")
    print(f"{len(found)} imports cross a seam the wrong way")
    ahead = [t for t in cfg.get("targets") or [] if int(t["at_most"]) < len(found)]
    if ahead:
        nxt = min(ahead, key=lambda t: str(t["by"]))
        print(f"next target: at most {nxt['at_most']} by {nxt['by']} ({len(found) - int(nxt['at_most'])} to go)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
