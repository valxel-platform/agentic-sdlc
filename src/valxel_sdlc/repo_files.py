"""The files the platform manages in every repo: where the platform's copy of each is, a copy another
repo owns (github:<owner>/<repo>/<path>), and the contract files a repo keeps.

The evaluators, intake's product setup and delivery all read these, so they're the core's
(ADR 0005); finding and fixing a repo's drift from the standards stays in repo_standards.py.
"""

from __future__ import annotations


def contracts_for(std, governance: str | None) -> dict:
    """The contract files a repo keeps, and whose copy each must match: the platform's for the
    platform's repos (governance None), the product's governance repo's for a product's lanes. A
    product's contracts are its own: the platform never holds or compares against them."""
    paths = dict((std.defaults or {}).get("contracts") or {})
    if governance is None:
        return paths
    return {path: f"{REMOTE}{governance}/{path}" for path in paths}


def standard_file_path(name: str):
    """Where the platform's copy of a managed file is, for reading it in place."""
    from pathlib import Path

    return Path(__file__).with_name("standard_files") / name


def standard_file(name: str) -> str:
    """The platform's copy of a managed file (src/valxel_sdlc/standard_files/)."""
    from importlib.resources import files  # nosemgrep: python37-compatibility-importlib2 -- requires-python >=3.11

    if name == "datasets.py":  # the module itself: governance CI runs the same checks as the evaluator
        return (files("valxel_sdlc") / "datasets.py").read_text()
    return (files("valxel_sdlc") / "standard_files" / name).read_text()


REMOTE = "github:"  # a managed file whose owner is another repo: github:<owner>/<repo>/<path>


def remote_source(name: str) -> tuple[str, str] | None:
    """(repo, path) for a managed file another repo owns (the audit schema is ss-audit-sink's)."""
    if not name.startswith(REMOTE):
        return None
    owner, repo, path = name[len(REMOTE):].split("/", 2)
    return f"{owner}/{repo}", path


def managed_content(gh, name: str) -> str | None:
    """The copy a repo must carry: the platform's, or the owning repo's default branch (a product's
    governance repo for its contracts). None if there's none to compare with (the product keeps no
    such contract, or it can't be read now): no drift is reported for it then."""
    src = remote_source(name)
    if src is None:
        return standard_file(name)
    try:
        return gh.get_file(repo=src[0], path=src[1], ref="")
    except Exception:  # noqa: BLE001 - checked again next time
        return None
