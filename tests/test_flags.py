"""Feature flags in each product's own Flipt (v2): the flag is created when G4 approves (off in
production, on before it), the coder builds behind it, G6 or a launch turns it on, on-call can turn
it off. Without a flag service features ship without one and a launch holds them after staging."""

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance.errors import GovernanceError
from vrixel_sdlc.graph.phase import flag_findings
from vrixel_sdlc.integrations.flags import FliptError, FliptFlags, NoFlags, ProductFlags, flag_key
from vrixel_sdlc.ops.signals import from_alertmanager
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
AG_FLAGS = """
  flags:
    provider: flipt
    url: https://flipt.agtech.example.com/
"""


class FakeFlipt:
    """Flipt v2's environments API as the adapter uses it: namespaces and flag resources per
    environment, one revision per environment, and 409 for a stale revision."""

    def __init__(self, envs=("dev", "staging", "production")):
        self.envs = {e: {"rev": 1, "namespaces": {"default"}, "flags": {}} for e in envs}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.conflict_next = False

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")  # api v2 environments {env} namespaces [ns resources ...]
        e = self.envs.get(parts[3])
        if e is None:
            return 404, {"message": "environment not found"}
        rev = str(e["rev"])
        if len(parts) == 5:
            if method == "GET":
                return 200, {"items": [{"key": n} for n in sorted(e["namespaces"])], "revision": rev}
            if body["revision"] != rev:
                return 409, {"message": "revision mismatch"}
            e["namespaces"].add(body["key"])
            e["rev"] += 1
            return 200, {"namespace": {"key": body["key"]}, "revision": str(e["rev"])}
        ns = parts[5]
        if ns not in e["namespaces"]:
            return 404, {"message": "namespace not found"}
        if method == "GET":
            flag = e["flags"].get((ns, parts[8]))
            return (200, {"resource": {"key": parts[8], "payload": dict(flag)}, "revision": rev}) if flag \
                else (404, {"message": "not found"})
        if self.conflict_next or body["revision"] != rev:
            self.conflict_next = False
            return 409, {"message": "revision mismatch"}
        exists = (ns, body["key"]) in e["flags"]
        if (method == "POST") == exists:
            return 400, {"message": "exists" if exists else "not found"}
        payload = {k: v for k, v in body["payload"].items() if v is not False}  # protobuf JSON drops false
        e["flags"][(ns, body["key"])] = payload
        e["rev"] += 1
        return 200, {"resource": {"key": body["key"], "payload": payload}, "revision": str(e["rev"])}

    def enabled(self, env, key, ns="ag"):
        flag = self.envs[env]["flags"].get((ns, key))
        return None if flag is None else bool(flag.get("enabled", False))


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def with_flipt(gov_root, extra=AG_FLAGS):
    p = gov_root / "vrixel-agtech" / "governance" / "product.yaml"
    p.write_text(p.read_text().replace("tools:\n", "tools:" + extra, 1))


def make(tmp_path, gov_root, *, flipt=None, live=False, token="t0ken"):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    if flipt is not None or live:
        def product_flags(pid):
            p = svc.snapshot(pid).product
            return p.tools.get("flags") or {}, p.runtime or {}
        svc.flags = ProductFlags(product_flags, live=live, env={"VRIXEL_FLIPT_TOKEN_AG": token} if token else {},
                                 transport_for=lambda url, tok: flipt)
    return FeatureEngine(svc, InMemorySaver()), svc


def gate(eng, tid, name):
    return next(p for p in eng.pending(tid) if p.get("gate") == name)


def drive_to(eng, fid, last_gate):
    """Approve gates until `last_gate` is the one waiting."""
    for _ in range(20):
        p = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not p or p[0]["gate"] == last_gate:
            return p[0] if p else None
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})


# --------------------------------------------------------------------------- the Flipt adapter


def test_flipt_creates_the_flag_off_in_production_and_on_before_it():
    fake = FakeFlipt()
    f = FliptFlags(fake, namespace="ag", environments={"dev": "dev", "staging": "staging", "production": "production"})
    f.ensure("ag.ag-feat-1.crop-log", name="AG-FEAT-1 Crop log", description="x", actor="arch")
    assert [fake.enabled(e, "ag.ag-feat-1.crop-log") for e in ("dev", "staging", "production")] == [True, True, False]
    assert all("ag" in fake.envs[e]["namespaces"] for e in fake.envs)  # the product's namespace was created
    body = next(b for m, p, b in fake.calls if m == "POST" and p.endswith("/resources"))
    assert body["payload"]["@type"] == "flipt.core.Flag" and body["payload"]["type"] == "BOOLEAN_FLAG_TYPE"

    fake.envs["dev"]["flags"][("ag", "ag.ag-feat-1.crop-log")]["enabled"] = False  # someone turned it off
    f.ensure("ag.ag-feat-1.crop-log", name="AG-FEAT-1 Crop log", description="x", actor="arch")
    assert fake.enabled("dev", "ag.ag-feat-1.crop-log") is False  # ensure never changes an existing flag

    assert f.get("ag.ag-feat-1.crop-log") is False
    f.set("ag.ag-feat-1.crop-log", True, actor="rm")
    assert f.get("ag.ag-feat-1.crop-log") is True and fake.enabled("staging", "ag.ag-feat-1.crop-log") is True
    meta = fake.envs["production"]["flags"][("ag", "ag.ag-feat-1.crop-log")]["metadata"]
    assert meta["changed_by"] == "rm" and meta["approved_by"] == "arch"


def test_flipt_retries_a_stale_revision_once_and_reports_missing_flags():
    fake = FakeFlipt()
    f = FliptFlags(fake, namespace="ag", environments={"dev": "dev", "production": "production"})
    f.ensure("k", name="n", description="d", actor="a")
    fake.conflict_next = True
    f.set("k", True, actor="rm")
    assert f.get("k") is True
    puts = [b for m, p, b in fake.calls if m == "PUT"]
    assert len(puts) == 2 and puts[0]["revision"] == puts[1]["revision"]  # refused once, re-read, written
    with pytest.raises(FliptError, match="doesn't exist"):
        f.set("missing", True, actor="rm")
    assert f.get("missing") is None


def test_flipt_environments_can_be_renamed():
    fake = FakeFlipt(envs=("development", "prod"))
    f = FliptFlags(fake, namespace="ag", environments={"dev": "development", "production": "prod"})
    f.ensure("k", name="n", description="d", actor="a")
    assert fake.enabled("development", "k") is True and fake.enabled("prod", "k") is False


def test_flag_keys_are_valid_flipt_keys():
    assert flag_key("EMR", "EMR-FEAT-3", "Check-in kiosk: walk-ins & QR!") == "emr.emr-feat-3.check-in-kiosk-walk-ins-qr"


# --------------------------------------------------------------------------- per-product routing


def test_products_route_to_their_own_flipt_or_fall_back(tmp_path, gov_root):
    with_flipt(gov_root)
    fake = FakeFlipt()
    _, svc = make(tmp_path, gov_root, flipt=fake, live=True)
    assert svc.flags.describe("AG") == "flipt" and svc.flags.available("AG")
    assert svc.flags.describe("EMR") == "none" and not svc.flags.available("EMR")  # live, no Flipt
    assert isinstance(svc.flags.for_product("EMR"), NoFlags)
    _, svc = make(tmp_path, gov_root, flipt=fake, live=False)
    assert svc.flags.describe("EMR") == "stub"  # stub delivery keeps in-memory flags
    _, svc = make(tmp_path, gov_root, flipt=fake, live=True, token="")
    assert svc.flags.describe("AG") == "none"  # configured, but the platform holds no token


def test_product_yaml_validates_the_flag_service(tmp_path, gov_root):
    with_flipt(gov_root, "\n  flags:\n    provider: flipt\n    url: https://flipt.agtech.example.com\n"
                         "    environments: {dev: development, qa: qa}\n")
    _, svc = make(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match="not in runtime.pipeline"):
        svc.snapshot("AG")

    shutil.rmtree(gov_root)
    shutil.copytree(ROOT / "examples" / "governance", gov_root)
    with_flipt(gov_root, "\n  flags:\n    provider: launchdarkly\n")
    with pytest.raises(GovernanceError, match="must be one of"):
        make(tmp_path, gov_root)[1].snapshot("AG")

    shutil.rmtree(gov_root)
    shutil.copytree(ROOT / "examples" / "governance", gov_root)
    with_flipt(gov_root)
    spec = make(tmp_path, gov_root)[1].snapshot("AG").product.tools["flags"]
    assert spec == {"provider": "flipt", "url": "https://flipt.agtech.example.com", "namespace": "ag",
                    "environments": {"dev": "dev", "staging": "staging", "production": "production"}}


# --------------------------------------------------------------------------- the pipeline


def test_g4_creates_the_flag_and_the_build_uses_it_then_g6_turns_it_on(tmp_path, gov_root):
    with_flipt(gov_root)
    fake = FakeFlipt()
    eng, svc = make(tmp_path, gov_root, flipt=fake, live=True)
    fid = eng.create(product_id="AG", title="Offline crop log")
    key = flag_key("AG", fid, "Offline crop log")
    assert drive_to(eng, fid, "g6")["gate"] == "g6"  # low risk: no G5
    v = eng.view(fid)
    assert [fake.enabled(e, key) for e in ("dev", "staging", "production")] == [True, True, False]
    assert any(e["kind"] == "flag" and key in e["detail"] for e in v["events"])
    assert all(f"Feature flag: {key}" in pr["body"] for pr in svc.github.prs.values()
               if pr["branch"] == f"feature/{fid}")  # every repo users reach is built behind it
    v = eng.auto_drive(fid)
    assert v["status"] == "done" and v["release"]["flag"] == key and v["release"]["flag_on"] is True
    assert fake.enabled("production", key) is True


def test_the_build_must_use_the_flag():
    ctx = {"flag": "ag.ag-feat-1.crop-log"}
    ok = {"TASK-1": {"role": "provider", "repo": "o/api", "tests_passed": True, "summary": "Feature flag: ag.ag-feat-1.crop-log"}}
    lib = {"TASK-1": {"role": "library", "repo": "o/lib", "tests_passed": True, "summary": "contracts"}}
    bad = {"TASK-1": {"role": "consumer", "repo": "o/web", "tests_passed": True, "summary": "no flag here"}}
    assert flag_findings(ctx, ok) == [] and flag_findings(ctx, lib) == [] and flag_findings({"flag": ""}, bad) == []
    assert flag_findings(ctx, bad) == ["No task builds behind feature flag ag.ag-feat-1.crop-log: gate the new behavior in o/web"]


def test_without_a_flag_service_a_feature_ships_without_a_flag(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root, flipt=FakeFlipt(), live=True)  # AG has no tools.flags
    fid = eng.create(product_id="AG", title="Offline crop log")
    v = eng.auto_drive(fid)
    assert v["status"] == "done" and v["release"]["flag"] == "" and v["release"]["flag_on"] is None
    assert any(e["kind"] == "flag" and "No flag service" in e["detail"] for e in v["events"])


def test_without_a_flag_service_a_launch_holds_its_features_after_staging(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root, flipt=FakeFlipt(), live=True)
    a = eng.create(product_id="AG", title="Offline crop log")
    b = eng.create(product_id="AG", title="Soil sensor import")
    rid = eng.create_release(product_id="AG", title="Spring season", model="launch", features=[a, b])
    for f in (a, b):
        v = eng.auto_drive(f)
        [w] = [p for p in v["pending"] if p["type"] == "external"]
        assert w["wait"] == "release_train" and "can't ship dark" in w["label"]
    g = gate(eng, rid, "release")
    eng.decide(rid, g["interrupt_id"], {"decision": "approve", "approver": "rm"})
    r = eng.view(rid)
    assert r["status"] == "shipped"
    for f in (a, b):
        v = eng.view(f)
        assert v["status"] == "done" and v["release"]["flag_on"] is None


def test_a_launch_with_flipt_ships_dark_then_turns_flipt_flags_on(tmp_path, gov_root):
    with_flipt(gov_root)
    fake = FakeFlipt()
    eng, _ = make(tmp_path, gov_root, flipt=fake, live=True)
    a = eng.create(product_id="AG", title="Offline crop log")
    rid = eng.create_release(product_id="AG", title="Spring season", model="launch", features=[a])
    v = eng.auto_drive(a)
    key = v["release"]["flag"]
    assert v["release"]["production"] and v["release"]["flag_on"] is False and fake.enabled("production", key) is False
    eng.decide(rid, gate(eng, rid, "release")["interrupt_id"], {"decision": "approve", "approver": "rm"})
    assert fake.enabled("production", key) is True and eng.view(a)["release"]["flag_on"] is True


def test_on_call_turns_a_flipt_flag_off(tmp_path, gov_root):
    with_flipt(gov_root)
    fake = FakeFlipt()
    eng, _ = make(tmp_path, gov_root, flipt=fake, live=True)
    fid = eng.create(product_id="AG", title="Offline crop log")
    key = eng.auto_drive(fid)["release"]["flag"]
    [iid] = eng.ingest(from_alertmanager({"alerts": [{
        "status": "firing", "fingerprint": "f", "startsAt": "2026-11-03T21:00:00Z",
        "labels": {"alertname": "Errors", "severity": "critical", "product": "AG"},
        "annotations": {"summary": "errors"}}]}))
    g = gate(eng, iid, "incident")
    assert ("flag_off", fid) in [(x["type"], x.get("feature_id")) for x in g["actions"]]
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert fake.enabled("production", key) is False and fake.enabled("staging", key) is True
