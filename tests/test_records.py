"""Approved documents go to the product's governance repo (valxel_sdlc.records, tracker T-54)."""

from conftest import approve

from valxel_sdlc import records


def test_each_approved_gate_commits_its_documents_to_the_governance_repo(engine, services):
    fid = engine.start(product_id="FARM", title="Farmer login", description="Add auth for farmer accounts")["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    repo = services.settings.entry("FARM").governance.full_name
    files = {(b, p): c for (r, b, p), c in services.github.files.items() if r == repo}
    g2 = {p: c for (b, p), c in files.items() if b == f"valxel/records/{fid}-g2"}
    assert set(g2) == {f"features/{fid}/prd.md", f"features/{fid}/approvals/g2.md"}
    assert "Approved by: g2-owner" in g2[f"features/{fid}/approvals/g2.md"]
    assert f"features/{fid}/discovery.md" in {p for (b, p) in files if b == f"valxel/records/{fid}-g1"}
    prs = [p for p in services.github.prs.values() if p["repo"] == repo and p["branch"].startswith("valxel/records/")]
    assert len(prs) == 2 and all(p["state"] == "merged" for p in prs) and all("records" in p["labels"] for p in prs)
    done = [e for e in engine.view(fid)["events"] if e["kind"] == "documents_recorded"]
    assert [e["node"] for e in done] == ["g1", "g2"] and all(e["merged"] for e in done)
    # Only approved documents: nothing an agent said on the way.
    assert not any("transcript" in p or "session" in p for (_, p) in files)


def test_a_failure_to_record_never_holds_the_feature_up(engine, services, monkeypatch):
    def broken(**_):
        raise RuntimeError("governance repo unreachable")

    monkeypatch.setattr(services.github, "put_file", broken)
    fid = engine.start(product_id="FARM", title="Farmer login", description="Add auth for farmer accounts")["feature_id"]
    approve(engine, fid, "g1")
    [e] = [e for e in engine.view(fid)["events"] if e["kind"].startswith("documents_")]
    assert e["kind"] == "documents_not_recorded" and "unreachable" in e["detail"]
    assert any(p.get("gate") == "g2" for p in engine.pending(fid))


class _Store:
    def __init__(self, data):
        self.data = data

    def path(self, meta, *, feature_id):
        return self.data[meta["id"]]


class _Arts:
    def load(self, ref):
        return "# Discovery\n"


def test_attachments_of_sensitive_or_large_files_are_listed_not_committed(tmp_path):
    small, big = tmp_path / "a.png", tmp_path / "b.pdf"
    small.write_bytes(b"\x89PNG small")
    big.write_bytes(b"%PDF" + b"x" * 2048)
    svc = type("S", (), {"artifacts": _Arts(), "attachments": _Store({"a": small, "b": big})})()
    metas = [{"id": "a", "name": "screen.png", "size": small.stat().st_size, "sha256": "aa"},
             {"id": "b", "name": "old-spec.pdf", "size": big.stat().st_size, "sha256": "bb"}]
    state = {"feature_id": "F-1", "artifacts": {"OPP": {"id": "F-1.OPP.v1", "version": 1}}, "attachments": metas,
             "data_class": "general"}
    files, left = records.files_for(svc, state, "g1", env={"VALXEL_RECORD_ATTACHMENT_MB": "0.001"})
    assert files["features/F-1/attachments/a-screen.png"] == b"\x89PNG small"
    assert [x["name"] for x in left] == ["old-spec.pdf"] and left[0]["why"] == "larger than the limit"
    files, left = records.files_for(svc, {**state, "data_class": "phi"}, "g1")
    assert set(files) == {"features/F-1/discovery.md"} and len(left) == 2
    text = records.approval_record(state, "g1", "pat", files, left, "2026-11-18T00:00:00+00:00")
    assert "sha256 aa" in text and "data class is phi" in text
