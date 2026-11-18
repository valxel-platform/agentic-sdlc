"""A thread's files are kept, and checked, per workspace (valxel_sdlc.tenancy: files), as its rows are."""

import pytest

from valxel_sdlc import tenancy
from valxel_sdlc.tenancy import PLATFORM, WorkspaceDenied, bound, thread_folder


@pytest.fixture
def store(services, tmp_path, monkeypatch):
    from valxel_sdlc.artifacts import ArtifactStore

    monkeypatch.setenv("VALXEL_TENANCY", "rls")
    return ArtifactStore(tmp_path)


def _save(store, fid, text="Body"):
    return store.save(feature_id=fid, kind="HLD", phase="design", content=text, parents=[], constitution_version="v1")


def test_files_are_kept_in_their_workspaces_folder(store, tmp_path):
    with bound(PLATFORM):
        ref = _save(store, "CLN-FEAT-1")
        plat = _save(store, "INCEPTION-INIT-1")
    assert ref["uri"] == str(tmp_path / "wrkspc-clinic" / "CLN-FEAT-1" / "HLD.v1.md")
    assert plat["uri"].startswith(str(tmp_path / "_platform" / "INCEPTION-INIT-1"))


def test_a_scope_reads_and_writes_only_its_own_workspaces_files(store):
    with bound(PLATFORM):
        clinic = _save(store, "CLN-FEAT-1", "PHI product design")
    with bound("wrkspc-clinic"):
        assert store.load(clinic) == "PHI product design"
        _save(store, "CLN-FEAT-2")
    with bound(["wrkspc-shared", "wrkspc-pay"]):
        with pytest.raises(WorkspaceDenied):
            store.load(clinic)
        with pytest.raises(WorkspaceDenied):
            _save(store, "CLN-FEAT-3")
        _save(store, "FARM-FEAT-1")  # its own
    with pytest.raises(WorkspaceDenied):  # no scope at all: nothing, as with rows
        store.load(clinic)


def test_a_ref_cant_point_outside_its_thread(store, tmp_path):
    with bound(PLATFORM):
        farm = _save(store, "FARM-FEAT-1")
        clinic = _save(store, "CLN-FEAT-1", "PHI product design")
    with bound("wrkspc-shared"):  # a ref naming its own thread, with another workspace's file
        forged = {**farm, "uri": clinic["uri"]}
        with pytest.raises(WorkspaceDenied):
            store.load(forged)
        sneaky = {**forged, "uri": str(tmp_path / "wrkspc-shared" / "FARM-FEAT-1" / ".." / ".." / "wrkspc-clinic"
                                      / "CLN-FEAT-1" / "HLD.v1.md")}
        with pytest.raises(WorkspaceDenied):
            store.load(sneaky)


def test_transcripts_and_attachments_follow_the_same_rule(tmp_path, monkeypatch, services):
    from valxel_sdlc.attachments import AttachmentStore
    from valxel_sdlc.sessions import TranscriptStore

    monkeypatch.setenv("VALXEL_TENANCY", "rls")
    src = tmp_path / "t.jsonl"
    src.write_text("{}")
    transcripts = TranscriptStore(tmp_path / "a")
    with bound("wrkspc-shared"):
        assert transcripts.keep("FARM-FEAT-1", "s-1", src)
        with pytest.raises(WorkspaceDenied):
            transcripts.keep("CLN-FEAT-1", "s-1", src)
        with pytest.raises(WorkspaceDenied):
            AttachmentStore(tmp_path / "a")._feature_dir("CLN-FEAT-1")
    with bound(PLATFORM):
        assert (thread_folder(tmp_path / "a", "FARM-FEAT-1") / "sessions" / "s-1.jsonl.gz").is_file()


def test_without_row_level_security_the_layout_holds_and_nothing_is_refused(services, tmp_path, monkeypatch):
    from valxel_sdlc.artifacts import ArtifactStore

    monkeypatch.delenv("VALXEL_TENANCY", raising=False)
    ref = _save(ArtifactStore(tmp_path), "CLN-FEAT-1")  # a laptop: no scope, no refusal
    assert "/wrkspc-clinic/CLN-FEAT-1/" in ref["uri"] and tenancy.may_touch("anything")
