"""The cloud ports (ADR 0010): every fake passes its port's conformance checks, the checks catch
what a broken adapter would do, and a product names its cloud in runtime.cloud."""

from datetime import UTC, datetime

import pytest

from valxel_sdlc.cloud import UnknownCloud, conformance, fakes, ports, provider


def test_every_fake_conforms():
    secrets = fakes.MemorySecrets()
    assert conformance.secret_store(secrets, set_value=secrets.rotate) == []
    signer = fakes.MemorySigner()
    assert conformance.signer(signer, public_pem=signer.public_pem) == []
    assert conformance.anchor_store(fakes.MemoryAnchors()) == []
    assert conformance.immutable_store(fakes.MemoryImmutable()) == []
    idp = fakes.FakeIdentity()
    assert conformance.identity_provider(idp, good_token=idp.token(sub="u1", email="ana@acme.test", groups=["eng"]),
                                         foreign_token=fakes.FakeIdentity("https://other.invalid").token(sub="x")) == []
    accounts = fakes.FakeAccounts({("ws1", "123"): "ext-1"})
    assert conformance.cloud_accounts(accounts, workspace="ws1", account="123", external_id="ext-1") == []
    def row(day, feature, service, usd):
        return {"day": day, "account": "123", "product_id": "FARM", "feature_id": feature, "service": service,
                "usd": usd}

    costs = fakes.FakeCosts(cloud=[row("2026-11-20", "FARM-7", "db", 1.5), row("2026-11-20", "FARM-7", "db", 0.5),
                                   row("2026-11-20", "FARM-8", "queue", 9), row("2026-11-21", "FARM-7", "db", 4)])
    assert conformance.cost_source(costs, product_id="FARM", feature_id="FARM-7", account="123", day="2026-11-20",
                                   expect={"db": 2.0}) == []
    inv = fakes.FakeInventory([
        {"account": "123", "id": "t1", "type": "key-value-table", "labels": {"valxel_product": "FARM"}},
        {"account": "123", "id": "t2", "type": "queue", "labels": {"valxel_product": "DENT"}}])
    assert conformance.resource_inventory(inv, account="123", product_id="FARM", expect_ids={"t1"}) == []
    sig = fakes.FakeSignals()
    assert conformance.signal_source(sig, firing={"kind": "fake-alarm", "name": "a", "firing": True},
                                     resolved={"kind": "fake-alarm", "name": "a", "firing": False},
                                     unrelated={"kind": "other"}) == []


def test_the_checks_catch_a_broken_adapter():
    class Leaky(fakes.MemoryImmutable):
        def put(self, key, data, *, retain_days):
            self.objects[key] = (bytes(data), datetime.now(UTC))  # overwrites: no lock

    assert any("written once" in p for p in conformance.immutable_store(Leaky()))

    class Forgetful(fakes.MemorySecrets):
        def rotate(self, name, value):
            self.values[name] = [value]  # drops the previous version

    s = Forgetful()
    assert any("[current, previous]" in p for p in conformance.secret_store(s, set_value=s.rotate))

    other = fakes.MemorySigner()
    assert conformance.signer(fakes.MemorySigner(), public_pem=other.public_pem)

    class Trusting(fakes.FakeIdentity):
        def claims(self, id_token):
            return {"iss": self.issuer}

    idp = Trusting()
    assert conformance.identity_provider(idp, good_token=idp.token(), foreign_token="fake.e30=")


def test_the_existing_interfaces_are_the_ports():
    from valxel_sdlc import audit_anchors, secret_ring, signing
    from valxel_sdlc.ops import cost

    assert secret_ring.SecretStore is ports.SecretStore and signing.Signer is ports.Signer
    assert audit_anchors.AnchorStore is ports.AnchorStore and cost.CostSource is ports.CostSource
    assert isinstance(secret_ring.EnvStore({}), ports.SecretStore)
    assert isinstance(audit_anchors.MemoryAnchors(), ports.AnchorStore)
    assert isinstance(cost.StubCostSource(), ports.CostSource)


def test_the_installation_has_one_cloud():
    assert provider({}) == "aws" and provider({"VALXEL_CLOUD": " GCP "}) == "gcp"
    with pytest.raises(UnknownCloud):
        provider({"VALXEL_CLOUD": "oracle"})


def _farm(settings):
    from pathlib import Path

    import yaml

    path = Path(settings.governance_root) / "acme-farm" / "farm-governance" / "product.yaml"
    raw = yaml.safe_load(path.read_text())
    return raw, settings.registry["FARM"]


def _parse(settings, raw, entry):
    import yaml

    from valxel_sdlc.governance.manifest import parse_manifest

    return parse_manifest(yaml.safe_dump(raw), entry, settings.limits, settings.registry, settings.tools)[0]


def test_runtime_aws_still_reads_as_the_aws_cloud(settings):
    raw, entry = _farm(settings)
    aws = raw["runtime"]["aws"]
    rt = _parse(settings, raw, entry).runtime
    assert rt["cloud"] == {"provider": "aws", "region": aws["region"],
                           "accounts": {str(k): str(v) for k, v in aws["accounts"].items()}}
    assert rt["aws"] == {"region": aws["region"], "accounts": rt["cloud"]["accounts"]}
    raw["runtime"]["cloud"] = {"provider": "aws", **raw["runtime"].pop("aws")}
    assert _parse(settings, raw, entry).runtime["aws"] == rt["aws"]


def test_runtime_cloud_is_checked(settings):
    from valxel_sdlc.governance.manifest import GovernanceError

    raw, entry = _farm(settings)
    raw["runtime"]["cloud"] = {"provider": "aws", "region": "us-east-1"}
    with pytest.raises(GovernanceError, match=r"runtime\.cloud and runtime\.aws both set"):
        _parse(settings, raw, entry)
    raw["runtime"].pop("aws")
    raw["runtime"]["cloud"] = {"provider": "oracle"}
    with pytest.raises(GovernanceError, match=r"runtime\.cloud\.provider is one of aws, gcp, azure"):
        _parse(settings, raw, entry)
    raw["runtime"]["cloud"] = {"provider": "gcp", "region": "us-central1", "accounts": {"production": "farm-prod"}}
    rt = _parse(settings, raw, entry).runtime
    assert rt["cloud"]["provider"] == "gcp" and rt["aws"] is None
