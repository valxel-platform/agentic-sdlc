"""Dependencies from shared events (T-108) and from the build's code (T-109)."""

from __future__ import annotations

from test_release_planning import gov_root  # noqa: F401
from test_roadmap import make

from valxel_sdlc.graph.phase import code_dependencies
from valxel_sdlc.undo import code_uses, derived, introduces

PUBLISHER = """# HLD: Walk-in queue
## Events
- valxel.queue.joined.v1: published by acme-clinic/cln-intake-api, applied by cln-queue-worker
- valxel.visit.booked.v1: published by acme-clinic/cln-intake-api, applied by cln-queue-worker
"""
APPLIER = """# HLD: SMS when called
## Events
- valxel.queue.joined.v1: published by acme-clinic/cln-intake-api, applied by cln-sms-channel
"""


def test_an_event_another_feature_introduced_is_a_dependency():
    # visit.booked was released before: it belongs to no feature
    i = introduces(PUBLISHER, released_events=["valxel.visit.booked.v1"])
    assert i["events"] == ["valxel.queue.joined.v1"]
    others = [{"feature_id": "CLN-FEAT-4", "title": "Walk-in queue", "introduces": i}]
    assert derived(APPLIER, others) == {"CLN-FEAT-4": ["event valxel.queue.joined.v1, which it introduces"]}
    assert derived(APPLIER.replace("queue.joined", "visit.booked"), others) == {}


OTHERS = [{"feature_id": "CLN-FEAT-4", "title": "Walk-in queue",
           "introduces": {"tables": ["queue_entry", "log"], "events": ["valxel.queue.joined.v1"], "repos": [],
                          "versions": {}}}]


def test_the_code_names_what_another_feature_introduced():
    code = 'SELECT position FROM queue_entry WHERE id = $1; publish("valxel.queue.joined.v1")\nlog.info("x")'
    assert code_uses(code, OTHERS) == {"CLN-FEAT-4": ["table queue_entry", "event valxel.queue.joined.v1"]}
    assert code_uses("queue_entry_archive and valxel.queue.joined.v10", OTHERS) == {}  # whole names only


def test_g5_sends_an_unlisted_dependency_back_to_the_architecture(tmp_path, gov_root):  # noqa: F811
    _, svc = make(tmp_path, gov_root)
    svc.github.put_file(repo="acme-clinic/cln-sms", branch="feature/CLN-FEAT-9", path="src/notify.py",
                        content='rows = db.query("SELECT * FROM queue_entry")\n', message="TASK-1")
    results = {"TASK-1": {"repo": "acme-clinic/cln-sms", "files": ["src/notify.py"], "tests_passed": True,
                          "branch": "feature/CLN-FEAT-9"}}
    ctx = {"other_designs": OTHERS, "depends_on": [], "branch": "feature/CLN-FEAT-9"}
    [issue] = code_dependencies(svc, ctx, results)
    assert issue["document"] == "architecture" and issue["section"] == "Depends on features"
    assert "table queue_entry from CLN-FEAT-4 (Walk-in queue)" in issue["problem"]
    assert issue["evidence"] == "acme-clinic/cln-sms/src/notify.py"
    assert code_dependencies(svc, {**ctx, "depends_on": ["CLN-FEAT-4"]}, results) == []
