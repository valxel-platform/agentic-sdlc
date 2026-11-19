"""The laptop tier's delivery (lite/local.py): images built from GitHub on this machine's Docker,
every product deployed through the Compose runtime here."""

import subprocess
from pathlib import Path

import pytest

from valxel_sdlc.lite.docker import StubDocker
from valxel_sdlc.lite.local import LocalBuilds, LocalRouter


class Tokens:
    def __init__(self):
        self.asked = []

    def token(self, org):
        self.asked.append(org)
        return "ghs_secret"


class FakeTools:
    """git and docker as a build would call them: records each call, writes the checkout's files."""

    def __init__(self, workload="metadata: {name: api}\nendpoints: [{name: http, port: 8080}]\n", fail=""):
        self.calls, self.workload, self.fail = [], workload, fail

    def __call__(self, args, cwd=None, env=None, **kw):
        self.calls.append((args, cwd, dict(env or {})))
        tool, verb = args[0], args[1]
        if verb == self.fail:
            return subprocess.CompletedProcess(args, 1, "", f"{verb} failed")
        if tool == "git" and verb == "checkout" and self.workload is not None:
            Path(cwd, "workload.yaml").write_text(self.workload)
        if tool == "git" and verb == "rev-parse":
            return subprocess.CompletedProcess(args, 0, "abc123def4567890\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def builds(tools, tmp_path):
    return LocalBuilds(Tokens(), runner=tools, workdir=tmp_path, background=False,
                       repo_of=lambda c: {"api": "acme-farm/farm-api"}.get(c, c))


def test_a_build_clones_the_commit_and_builds_an_image_named_by_it(tmp_path):
    tools = FakeTools()
    b = builds(tools, tmp_path)
    run = b.start(component="api", ref="main", kind="build", repo_url="")
    s = b.status(run_id=run["run_id"], component="api")
    assert s["status"] == "succeeded" and s["image"] == "valxel-local/api:abc123def456"  # gitleaks:allow (a fake commit)
    assert s["workload"]["endpoints"][0]["port"] == 8080
    fetch = next(c for c in tools.calls if c[0][:2] == ["git", "fetch"])
    assert fetch[0][-1] == "main" and "GIT_ASKPASS" in fetch[2]
    assert all("ghs_secret" not in " ".join(c[0]) for c in tools.calls)  # never on a command line
    build = next(c for c in tools.calls if c[0][:2] == ["docker", "build"])
    assert "valxel.commit=abc123def4567890" in build[0]
    assert not list(tmp_path.glob("valxel-builds/*"))  # the checkout, and the token, are gone


def test_a_failed_build_says_why(tmp_path):
    b = builds(FakeTools(fail="build"), tmp_path)
    run = b.start(component="api", ref="main", kind="build", repo_url="")
    s = b.status(run_id=run["run_id"], component="api")
    assert s["status"] == "failed" and "build failed" in s["message"]
    b = builds(FakeTools(workload=None), tmp_path)
    s = b.status(run_id=b.start(component="api", ref="main", kind="build", repo_url="")["run_id"], component="api")
    assert s["status"] == "failed" and "workload.yaml" in s["message"]


def test_refs_and_repos_are_checked(tmp_path):
    b = builds(FakeTools(), tmp_path)
    with pytest.raises(ValueError):
        b.start(component="api", ref="--upload-pack=x", kind="build", repo_url="")
    with pytest.raises(ValueError):
        b.start(component="api", ref="../../etc", kind="build", repo_url="")
    with pytest.raises(ValueError):
        b.start(component="not a repo", ref="main", kind="build", repo_url="")


def test_every_product_deploys_through_the_runtime_here(services, tmp_path, monkeypatch):
    monkeypatch.setenv("VALXEL_LITE_STATE_DIR", str(tmp_path / "lite"))
    docker = StubDocker()
    tools = FakeTools()
    router = LocalRouter(services.openchoreo, services, tokens=Tokens(), docker_for=lambda h: docker,
                         builds=LocalBuilds(Tokens(), runner=tools, workdir=tmp_path, background=False))
    for pid in ("FARM", "CLN"):  # a full-mode product too: on a laptop there's no OpenChoreo
        assert router.runtime_for(pid.lower()) is not None
    rt = router.runtime_for("farm")
    assert rt.publish_local  # external endpoints reachable from this machine's browser
    run = rt.run_workflow(component="acme-farm/farm-api", ref="main", kind="build", project="farm")
    s = rt.run_status(run_id=run["run_id"], component="acme-farm/farm-api", project="farm")
    assert s["status"] == "succeeded" and s["release"]
    fetch = next(c for c in tools.calls if c[0][:2] == ["git", "remote"])
    assert fetch[0][-1] == "https://github.com/acme-farm/farm-api.git"
