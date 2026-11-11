"""Shared-lane releases: Git tags drive the release workflows; GitHub Packages is checked at G6."""

import pytest

from vrixel_sdlc.integrations.packages import GitHubPackages, StubPackageRegistry, next_rc, next_version

REPO = "acme-shared/ss-audit-framework"


def test_versions_follow_the_latest_stable_tag():
    tags = ["v1.4.0", "v1.4.1", "v1.5.0-rc.1", "v1.5.0-rc.2", "v0.9.0", "nightly"]
    assert next_version(tags) == "1.5.0" and next_version(tags, "patch") == "1.4.2"
    assert next_version(tags, "major") == "2.0.0" and next_version([]) == "0.1.0"
    assert next_rc(tags, "1.5.0") == 3 and next_rc(tags, "1.6.0") == 1


class FakeRest:
    """The two GitHub calls the adapter makes: refs on the repo, and the org's package versions."""

    def __init__(self, tags=None, versions=None):
        self.tags = dict(tags or {})
        self.versions = versions
        self.created = []

    def _call(self, method, repo, path, body=None, *, ok404=False):
        if path == "/git/matching-refs/tags/v":
            return [{"ref": f"refs/tags/{t}", "object": {"sha": s}} for t, s in self.tags.items()]
        if path.startswith("/git/ref/tags/"):
            tag = path.rsplit("/", 1)[-1]
            return {"object": {"sha": self.tags[tag]}} if tag in self.tags else None
        if method == "POST" and path == "/git/refs":
            self.tags[body["ref"].removeprefix("refs/tags/")] = body["sha"]
            self.created.append(body)
            return {}
        raise AssertionError(path)

    def _abs(self, method, org, path, body=None, *, ok404=False):
        assert path.startswith(f"/orgs/{org}/packages/maven/com.acme.shared.audit-framework/versions")
        if isinstance(self.versions, Exception):
            raise self.versions
        return self.versions


def test_a_merge_tags_a_release_candidate_and_g6_tags_the_same_commit():
    rest = FakeRest({"v1.4.0": "old"})
    reg = GitHubPackages(rest)
    rc = reg.publish(repo=REPO, version=None, channel="rc", actor="agent:merge-train", sha="abc123")
    assert (rc["version"], rc["tag"], rc["package_version"]) == ("1.5.0", "v1.5.0-rc.1", "1.5.0-rc.1")
    again = reg.publish(repo=REPO, version="1.5.0", channel="rc", actor="x", sha="def456")  # a fix after a failure
    assert again["tag"] == "v1.5.0-rc.2"
    stable = reg.publish(repo=REPO, version="1.5.0", channel="stable", actor="rm", sha="def456")
    assert stable["tag"] == "v1.5.0" and rest.tags["v1.5.0"] == "def456"
    assert reg.publish(repo=REPO, version="1.5.0", channel="stable", actor="rm", sha="def456")["tag"] == "v1.5.0"
    assert len(rest.created) == 3  # the retry found its tag
    with pytest.raises(ValueError, match="already points at"):
        reg.publish(repo=REPO, version="1.5.0", channel="stable", actor="rm", sha="other")
    with pytest.raises(ValueError, match="needs the merge commit"):
        reg.publish(repo=REPO, version=None, channel="rc", actor="x")


def test_github_packages_says_whether_the_workflow_published_it():
    pkg = {"type": "maven", "name": "com.acme.shared.audit-framework"}
    assert GitHubPackages(FakeRest(versions=[{"name": "1.5.0-rc.1"}])).status(
        repo=REPO, version="1.5.0-rc.1", package=pkg) == "published"
    assert GitHubPackages(FakeRest(versions=[{"name": "1.4.0"}])).status(repo=REPO, version="1.5.0-rc.1", package=pkg) == "missing"
    assert GitHubPackages(FakeRest(versions=None)).status(repo=REPO, version="1.5.0-rc.1", package=pkg) == "missing"
    assert GitHubPackages(FakeRest(versions=RuntimeError("503"))).status(repo=REPO, version="1", package=pkg) == "unknown"
    assert GitHubPackages(FakeRest()).status(repo=REPO, version="1", package=None) == "unknown"


def test_g6_wont_mark_stable_a_candidate_that_was_never_published(engine, services):
    fid = engine.start(product_id="FARM", title="Audit trail for field edits",
                       description="Record who changed a field, with the shared audit framework")["feature_id"]
    for _ in range(40):
        ps = [p for p in engine.pending(fid) if p["type"] != "external"]
        ss = [p for p in ps if p.get("gate") == "g6" and p.get("packages") is not None]
        if ss:
            break
        if not ps:
            raise AssertionError("never reached the shared lane's G6")
        if ps[0].get("gate") == "g6" and not services.packages.missing:
            services.packages.missing.add((REPO, "0.1.0-rc.1"))  # its release workflow failed
        engine.decide(fid, ps[0]["interrupt_id"], {"decision": "approve", "approver": "x"})
    [g6] = ss
    assert g6["packages"][0] | {"status": "missing"} == g6["packages"][0]
    assert g6["packages"][0]["rc"] == "v0.1.0-rc.1"
    engine.decide(fid, g6["interrupt_id"], {"decision": "approve", "approver": "rm"})
    [again] = [p for p in engine.pending(fid) if p.get("gate") == "g6" and p.get("packages") is not None]
    assert again["refused"].startswith("Not in GitHub Packages yet: v0.1.0-rc.1")
    assert services.packages.channel(REPO, "0.1.0") == "rc"
    services.packages.missing.clear()  # the workflow ran again
    engine.decide(fid, again["interrupt_id"], {"decision": "approve", "approver": "rm"})
    assert engine.auto_drive(fid)["status"] == "done"
    assert services.packages.channel(REPO, "0.1.0") == "stable"


def test_the_stub_counts_every_candidate_as_published():
    reg = StubPackageRegistry()
    rc = reg.publish(repo=REPO, version=None, channel="rc", actor="a", sha="s", bump="major")
    assert rc["version"] == "1.0.0" and reg.status(repo=REPO, version=rc["package_version"]) == "published"
