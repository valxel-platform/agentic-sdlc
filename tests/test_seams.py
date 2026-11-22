"""The seams stay where they are: no new import across them, and the debt list only shrinks."""

from valxel_sdlc import seams


def test_no_new_import_crosses_a_seam():
    new, gone = seams.check()
    assert not new, ("These imports cross a seam (config/seams.yaml, ADR 0005). Move the shared piece into the "
                     "core, or pass it in from the root, rather than importing it:\n  " + "\n  ".join(new))
    assert not gone, ("These listed breaks are fixed: delete them from config/seams.yaml's known_breaks:\n  "
                      + "\n  ".join(gone))


def test_the_debt_meets_this_sprints_target():
    from datetime import UTC, datetime

    cfg = seams.load()
    n = len(seams.breaks(cfg))
    missed = seams.over_target(cfg, n, datetime.now(UTC).date().isoformat())
    assert missed is None, (f"{n} imports cross a seam; the target for {missed['by']} is at most {missed['at_most']} "
                            "(config/seams.yaml targets). Fix some (see `python -m valxel_sdlc.seams`) rather "
                            "than moving the target.")


def test_targets_are_dated_and_go_down():
    targets = seams.load().get("targets") or []
    assert targets and [str(t["by"]) for t in targets] == sorted(str(t["by"]) for t in targets)
    assert [t["at_most"] for t in targets] == sorted((t["at_most"] for t in targets), reverse=True)
    assert targets[-1]["at_most"] == 0
    cfg = {"targets": [{"by": "2026-01-01", "at_most": 10}, {"by": "2026-02-01", "at_most": 5}]}
    assert seams.over_target(cfg, 12, "2025-12-31") is None  # nothing due yet
    assert seams.over_target(cfg, 12, "2026-01-15")["at_most"] == 10
    assert seams.over_target(cfg, 7, "2026-01-15") is None
    assert seams.over_target(cfg, 7, "2026-02-01")["at_most"] == 5


def test_what_the_core_list_adds_uses_no_seam():
    """`core` in config/seams.yaml is for shared definitions only: one that imports a seam isn't."""
    cfg = seams.load()
    listed = cfg.get("core") or []
    bad = sorted(f"{a} -> {b}" for a, b in seams.imports()
                 if any(a == p or a.startswith(p + ".") for p in listed) and seams.seam_of(a, cfg) == "core"
                 and seams.seam_of(b, cfg) not in ("core",))
    assert not bad, f"config/seams.yaml lists these as core, but they use a seam: {bad}"


def test_every_listed_module_exists():
    cfg = seams.load()
    mods = {seams._module(p) for p in seams.ROOT.rglob("*.py")}
    prefixes = [p for ps in cfg["seams"].values() for p in ps] + cfg["root"] + (cfg.get("core") or [])
    missing = [p for p in prefixes if not any(m == p or m.startswith(p + ".") for m in mods)]
    assert not missing, f"config/seams.yaml names modules that don't exist: {missing}"


def test_the_rules():
    cfg = {"seams": {"ops": ["ops"], "delivery": ["graph"]}, "root": ["engine"]}
    found = seams.breaks(cfg, {("ops.incident", "graph.phase"), ("ops.incident", "tenancy"),
                               ("tenancy", "ops.signals"), ("engine", "ops.incident"), ("graph.phase", "graph.gates")})
    assert found == {"ops.incident -> graph.phase", "tenancy -> ops.signals"}
