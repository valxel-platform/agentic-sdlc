"""Images already running are rescanned: Amazon Inspector's findings on ECR images open incidents
for the component, and a closed finding resolves its signal."""

from __future__ import annotations

from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.ops.signals import from_inspector
from valxel_sdlc.web.auth import AuthConfig


def finding(sev="CRITICAL", status="ACTIVE", fix="YES", in_use=2, repo="acme-clinic/cln-intake-api", kind="PACKAGE_VULNERABILITY"):
    img = {"repositoryName": repo, "imageHash": "sha256:" + "a" * 64, "imageTags": ["4f2c1d0"]}
    if in_use is not None:
        img["inUseCount"] = in_use
    return {"version": "0", "detail-type": "Inspector2 Finding", "source": "aws.inspector2", "account": "333333333333",
            "time": "2026-11-12T10:00:00Z",
            "detail": {"findingArn": "arn:aws:inspector2:us-east-2:333333333333:finding/abc", "severity": sev,
                       "status": status, "type": kind, "title": "CVE-2026-1234 - openssl", "fixAvailable": fix,
                       "packageVulnerabilityDetails": {"vulnerabilityId": "CVE-2026-1234", "sourceUrl": "https://nvd.example/CVE-2026-1234",
                                                       "vulnerablePackages": [{"name": "openssl", "version": "3.0.1", "fixedInVersion": "3.0.9"}]},
                       "resources": [{"type": "AWS_ECR_CONTAINER_IMAGE", "details": {"awsEcrContainerImage": img}}]}}


def test_a_running_images_vulnerability_becomes_a_signal():
    [s] = from_inspector(finding())
    assert s["component"] == "cln-intake-api" and s["env"] == "production" and s["severity"] == "sev2"
    assert s["status"] == "firing" and "openssl 3.0.1 -> 3.0.9" in s["detail"] and s["category"] == "security"
    assert from_inspector(finding(sev="HIGH"))[0]["severity"] == "sev3"
    assert from_inspector(finding(fix="NO"))[0]["severity"] == "sev3"  # nothing to do yet but know
    assert from_inspector(finding(status="CLOSED"))[0]["status"] == "resolved"
    # Not running anywhere, medium, or not a package vulnerability: nothing.
    assert from_inspector(finding(in_use=0)) == [] and from_inspector(finding(sev="MEDIUM")) == []
    assert from_inspector(finding(kind="NETWORK_REACHABILITY")) == []
    assert from_inspector({"detail-type": "CloudWatch Alarm State Change"}) == []
    assert from_inspector(finding(in_use=None))  # Inspector didn't say: count it


def test_the_endpoint_wants_the_signals_token_and_opens_an_incident(engine, monkeypatch):
    client = TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", "t0k")
    assert client.post("/api/signals/inspector", json=finding()).status_code == 401
    r = client.post("/api/signals/inspector", json=finding(), headers={"authorization": "Bearer t0k"})
    assert r.status_code == 202 and r.json()["signals"] == 1
