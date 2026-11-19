"""The seams stay where they are: no new import across them, and the debt list only shrinks."""

from valxel_sdlc import seams


def test_no_new_import_crosses_a_seam():
    new, gone = seams.check()
    assert not new, ("These imports cross a seam (config/seams.yaml, ADR 0005). Move the shared piece into the "
                     "core, or pass it in from the root, rather than importing it:\n  " + "\n  ".join(new))
    assert not gone, ("These listed breaks are fixed: delete them from config/seams.yaml's known_breaks:\n  "
                      + "\n  ".join(gone))


def test_every_listed_module_exists():
    cfg = seams.load()
    mods = {seams._module(p) for p in seams.ROOT.rglob("*.py")}
    prefixes = [p for ps in cfg["seams"].values() for p in ps] + cfg["root"]
    missing = [p for p in prefixes if not any(m == p or m.startswith(p + ".") for m in mods)]
    assert not missing, f"config/seams.yaml names modules that don't exist: {missing}"


def test_the_rules():
    cfg = {"seams": {"ops": ["ops"], "delivery": ["graph"]}, "root": ["engine"]}
    found = seams.breaks(cfg, {("ops.incident", "graph.phase"), ("ops.incident", "tenancy"),
                               ("tenancy", "ops.signals"), ("engine", "ops.incident"), ("graph.phase", "graph.gates")})
    assert found == {"ops.incident -> graph.phase", "tenancy -> ops.signals"}
