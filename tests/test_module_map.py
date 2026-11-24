"""The module map (docs/module-map.html) places every module in a capability: a new module needs one."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "module_map.py"


def _module_map():
    spec = importlib.util.spec_from_file_location("module_map", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_module_is_in_exactly_one_capability_of_its_layer():
    data = _module_map().build()  # raises naming the module that has no capability, or two
    assert len(data["cap_of"]) == len(data["m"])
    assert all(c["mods"] for c in data["caps"])


def test_capability_links_only_run_down_or_within_a_layer():
    data = _module_map().build()
    rank = {"root": 0, "intake": 1, "delivery": 1, "roadmap": 1, "ops": 1, "core": 2}
    layer = {c["k"]: c["layer"] for c in data["caps"]}
    for link in data["links"]:
        a, b = layer[link["a"]], layer[link["b"]]
        assert rank[a] <= rank[b] and not (rank[a] == rank[b] == 1 and a != b), (link["a"], link["b"])
