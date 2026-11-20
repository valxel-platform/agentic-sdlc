"""Try the change before it merges (valxel_sdlc.preview): the change set's environment's web app, as
a link at G5 and on the feature, until it merges or its time to live runs out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from valxel_sdlc import preview

NOW = datetime(2026, 11, 20, 18, tzinfo=UTC)


def state(**over):
    s = {"tasks": [{"repo": "acme/web", "role": "consumer"}, {"repo": "acme/bff", "role": "bff"},
                   {"repo": "acme/api", "role": "provider"}],
         "change_set": {"env": {"name": "cs-cln-feat-3", "created": (NOW - timedelta(hours=2)).isoformat()}},
         "delivery": {"change_set": {"status": "success", "results": [
             {"repo": "acme/web", "url": "https://web.cs.example"}, {"repo": "acme/bff", "url": "https://bff.cs.example"},
             {"repo": "acme/api", "url": "https://api.cs.example"}]}}}
    return {**s, **over}


def test_the_web_app_through_its_bff_until_the_time_to_live_runs_out():
    got = preview.of(state(), 48, now=NOW)
    assert got == {"url": "https://bff.cs.example", "env": "cs-cln-feat-3",
                   "expires_at": (NOW + timedelta(hours=46)).isoformat()}
    assert preview.of(state(), 1, now=NOW) == {"gone": "Its environment ran out of time (1 hours) and was removed"}


def test_without_a_bff_the_web_app_itself():
    s = state(tasks=[{"repo": "acme/web", "role": "consumer"}, {"repo": "acme/api", "role": "provider"}])
    assert preview.of(s, 48, now=NOW)["url"] == "https://web.cs.example"


def test_nothing_to_try_or_gone():
    assert preview.of(state(tasks=[{"repo": "acme/api", "role": "provider"}]), 48, now=NOW) is None  # no web app
    assert preview.of(state(change_set={}), 48, now=NOW) is None  # no environment
    assert preview.of(state(delivery={"change_set": {"status": "failure"}}), 48, now=NOW) is None  # didn't pass
    gone = state(change_set={"env": {"name": "cs-x", "deleted": True}})
    assert "removed" in preview.of(gone, 48, now=NOW)["gone"]


def test_the_feature_view_carries_it(engine, monkeypatch):
    fid = engine.create(product_id="FARM", title="Field list sorting", description="Sort the field list by moisture")
    assert engine.view(fid)["try_it"] is None  # nothing built yet
    monkeypatch.setattr(preview, "for_feature", lambda services, s: {"url": "https://bff.cs.example", "env": "cs-x"})
    assert engine.view(fid)["try_it"]["url"] == "https://bff.cs.example"
