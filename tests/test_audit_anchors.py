"""Chain heads anchored where the tool can't rewrite them (engineering review finding 7, T-135)."""

import io
import json

from valxel_sdlc import audit_anchors as aa
from valxel_sdlc.audit_events import Auditor, InMemoryOutbox
from valxel_sdlc.ops import changes
from valxel_sdlc.workspaces import InMemoryWorkspaceStore, chained, digest, verify_chain


class Ws:
    def __init__(self):
        self.store = InMemoryWorkspaceStore()


def setup():
    ws, log = Ws(), changes.InMemoryChangeLog()
    ws.store.put("workspace", "acme", "", {"id": "acme"})
    for i in range(3):
        ws.store.audit("acme", "ann", "member.add", {"member": f"m{i}"})
    log.record(changes.new(key="c1", product_id="CLN", kind="flag", type="standard", action="off", summary="x",
                           targets=[]))
    return ws, log


def recompute(entries):
    """What someone who can disable the trigger would do: rewrite, then fix every later hash."""
    prev = "0" * 64
    for e in entries:
        e.update(chained(prev, {k: e[k] for k in ("workspace", "at", "actor", "action", "detail")}))
        prev = e["hash"]


def test_a_rewrite_with_recomputed_hashes_passes_the_chain_but_not_the_anchors():
    ws, log = setup()
    store = aa.MemoryAnchors()
    a = aa.Anchorer(store, ws, log, Auditor(InMemoryOutbox()))
    assert a.run() == 2 and a.run() == 0  # both chains anchored; nothing moved since
    rows = ws.store._audit
    rows[1]["actor"] = "mallory"
    recompute(rows)
    assert verify_chain(ws.store.audit_chain("acme"))["ok"]  # the chain alone can't tell
    found = aa.verify(store, ws, log)
    assert found["changes"] == [] and "rewritten" in found["workspace-acme"][0]


def test_cutting_off_the_tail_contradicts_the_anchor():
    ws, log = setup()
    store = aa.MemoryAnchors()
    aa.Anchorer(store, ws, log).run()
    del ws.store._audit[-1]
    assert "the tail was removed" in aa.verify(store, ws, log)["workspace-acme"][0]


def test_entries_after_the_last_anchor_are_fine_and_the_next_run_anchors_them():
    ws, log = setup()
    store = aa.MemoryAnchors()
    a = aa.Anchorer(store, ws, log)
    a.run()
    ws.store.audit("acme", "ann", "member.remove", {"member": "m0"})
    assert aa.verify(store, ws, log) == {"workspace-acme": [], "changes": []}
    assert a.run() == 1 and store.list("workspace-acme")[-1]["count"] == 4


def test_each_anchor_is_also_sent_to_the_audit_sink():
    ws, log = setup()
    audit = Auditor(InMemoryOutbox())
    aa.Anchorer(aa.MemoryAnchors(), ws, log, audit).run()
    sent = [e["data"] for e in audit.outbox.events()]
    assert {(d["action"], d["resource"]["ids"][0]) for d in sent} == {("audit.anchor", "workspace-acme"),
                                                                     ("audit.anchor", "changes")}
    assert all(len(d["details"]["hash"]) == 64 for d in sent)


def test_no_anchor_store_no_job():
    ws, log = setup()
    assert aa.Anchorer(None, ws, log).run() == 0


class FakeS3:
    def __init__(self):
        self.objects, self.puts = {}, []

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def list_objects_v2(self, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(kw["Prefix"])],
                "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def test_s3_anchors_are_locked_in_compliance_mode():
    s3, t = FakeS3(), [1_800_000_000.0]
    store = aa.S3Anchors("anchors-bucket", 30, s3=s3, clock=lambda: t[0])
    store.put("changes", {"count": 1, "hash": "a" * 64})
    t[0] += 3600
    store.put("changes", {"count": 2, "hash": "b" * 64})
    put = s3.puts[0]
    assert put["ObjectLockMode"] == "COMPLIANCE" and put["Key"].startswith("anchors/changes/")
    assert (put["ObjectLockRetainUntilDate"] - __import__("datetime").datetime.fromtimestamp(
        1_800_000_000, __import__("datetime").UTC)).days == 30
    assert [a["count"] for a in store.list("changes")] == [1, 2] and store.list("changes", 1)[0]["count"] == 2
    assert json.loads(s3.objects[put["Key"]])["chain"] == "changes"
    for bad in ("", "../x", "a/b"):
        try:
            store.put(bad, {"count": 1, "hash": "c"})
        except ValueError:
            continue
        raise AssertionError(bad)


def test_the_cli_reports_tampering_against_the_anchors(capsys):
    from valxel_sdlc.cli import _verify_audit

    ws, log = setup()
    store = aa.MemoryAnchors()
    aa.Anchorer(store, ws, log).run()
    assert _verify_audit(ws.store, log, store) == 0
    assert "every chain agrees with its anchors" in capsys.readouterr().out
    ws.store._audit[0]["actor"] = "mallory"
    recompute(ws.store._audit)
    assert _verify_audit(ws.store, log, store) == 1
    assert "workspace-acme: CONTRADICTS ITS ANCHORS" in capsys.readouterr().out
    assert digest  # the chain's own function, used above through chained()
