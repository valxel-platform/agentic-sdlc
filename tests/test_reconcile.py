"""Reconciliation in staging: each data contract's events, published by its owner, reach every
consumer, and none broke the contract. Counts only; all data synthetic."""

import json
from types import SimpleNamespace

from valxel_sdlc import reconcile

from test_datasets import notes

LINE = "valxel.datasets"


def entries(*rows):
    return [(c, f"{LINE} {json.dumps(d)}") for c, d in rows]


def test_summary_lines_are_summed_per_component_and_dataset():
    rows = entries(("emr-notes-api", {"emr.notes": {"published": 5}}), ("emr-notes-api", {"emr.notes": {"published": 2}}),
                   ("valxel-emr/emr-notes-worker", {"emr.notes": {"applied": 6, "duplicate": 1}}))
    rows += [("emr-knowledge", json.dumps({"message": f'{LINE} {{"emr.notes":{{"applied":7}}}}'})),
             ("x", "unrelated"), ("x", f"{LINE} not json"), ("x", f"{LINE} [1]"), ("x", f'{LINE} {{"d": 3}}')]
    counts = reconcile.parse(rows)
    assert counts["emr-notes-api"] == {"emr.notes": {"published": 7}}
    assert counts["emr-notes-worker"] == {"emr.notes": {"applied": 6, "duplicate": 1}}
    assert counts["emr-knowledge"] == {"emr.notes": {"applied": 7}}


def test_whatever_was_published_reached_every_consumer_and_nothing_broke_the_contract():
    good = reconcile.parse(entries(("emr-notes-api", {"emr.notes": {"published": 7}}),
                                   ("emr-notes-worker", {"emr.notes": {"applied": 6, "duplicate": 1}}),
                                   ("emr-knowledge", {"emr.notes": {"applied": 7}})))
    assert reconcile.problems([notes()], good) == []
    bad = reconcile.parse(entries(("emr-notes-api", {"emr.notes": {"published": 7, "refused": 1}}),
                                  ("emr-notes-worker", {"emr.notes": {"applied": 4, "parked": 1}})))
    found = {(p["repo"], p["message"]) for p in reconcile.problems([notes()], bad)}
    assert found == {
        ("valxel-emr/emr-notes-api", "refused 1 emr.notes events that broke the data contract"),
        ("valxel-emr/emr-notes-worker", "parked 1 emr.notes events that broke the data contract"),
        ("valxel-emr/emr-notes-worker", "received 5 of the 7 emr.notes events emr-notes-api published"),
        ("valxel-emr/emr-knowledge", "received 0 of the 7 emr.notes events emr-notes-api published")}
    assert reconcile.problems([notes()], {}) == []  # staging didn't exercise it


class Logs:
    def __init__(self, rows=None, broken=False):
        self.rows, self.broken, self.asked = rows or [], broken, {}

    def entries(self, **kw):
        if self.broken:
            raise RuntimeError("observer down")
        self.asked = kw
        return self.rows


def services(logs):
    snap = SimpleNamespace(product=SimpleNamespace(runtime={"namespace": "emr", "openchoreo_project": "emr"}))
    return SimpleNamespace(cohort_logs=logs, snapshot=lambda pid: snap)


def test_the_staging_check_reads_the_logs_since_the_staging_deploy():
    state = {"product_id": "EMR", "events": [{"node": "deploy_staging", "at": "2026-11-16T10:00:00+00:00"}]}
    logs = Logs(entries(("emr-notes-api", {"emr.notes": {"published": 2}}), ("emr-notes-worker", {"emr.notes": {"applied": 2}})))
    check = reconcile.staging_check(services(logs), state, [notes()], "staging")
    assert logs.asked["phrase"] == LINE and logs.asked["since"].isoformat().startswith("2026-11-16T10:00")
    assert check["checked"] and [p["repo"] for p in check["problems"]] == ["valxel-emr/emr-knowledge"]
    assert reconcile.describe(check).startswith("Datasets don't reconcile in staging: emr-knowledge received 0 of the 2")
    ok = reconcile.staging_check(services(Logs()), state, [notes()], "staging")
    assert reconcile.describe(ok) == "All 1 datasets reconcile in staging"
    down = reconcile.staging_check(services(Logs(broken=True)), state, [notes()], "staging")
    assert not down["checked"] and "observer down" in reconcile.describe(down)
    assert reconcile.staging_check(services(Logs()), state, [], "staging")["why"] == "the design names no data contracts"
    assert reconcile.describe({}) == ""


def test_without_a_log_source_the_check_says_so_or_shows_a_requested_gap():
    none = SimpleNamespace()
    state = {"product_id": "EMR"}
    assert "no log source" in reconcile.staging_check(none, state, [notes()], "staging")["why"]
    gap = reconcile.staging_check(none, {**state, "simulate_faults": {"datasets": 1}}, [notes()], "staging")
    assert gap["checked"] and gap["problems"][0]["repo"] == "valxel-emr/emr-knowledge"


PULLS = "valxel.pulls"
SOURCES = {"acme-emr/emr-labs": ["test-catalog", "protocols"]}


def pull_line(component, rec):
    return (component, f"{PULLS} {json.dumps(rec)}")


def test_pull_runs_are_summed_per_source_and_a_parked_record_or_a_source_that_never_succeeded_holds_the_release():
    rows = [pull_line("emr-labs", {"test-catalog": {"runs": 1, "succeeded": 1, "seen": 5, "parked": 1}}),
            pull_line("emr-labs", {"test-catalog": {"runs": 1, "succeeded": 1, "seen": 3}}),
            pull_line("emr-labs", {"protocols": {"runs": 2, "failed": 2}}),
            ("emr-labs", f'{LINE} {json.dumps({"emr.lab-test": {"published": 7}})}')]
    counts = reconcile.parse(rows, PULLS)
    assert counts["emr-labs"]["test-catalog"] == {"runs": 2, "succeeded": 2, "seen": 8, "parked": 1}
    assert "emr.lab-test" not in counts["emr-labs"]  # the datasets line is counted separately
    found = [p["message"] for p in reconcile.pull_problems(SOURCES, counts)]
    assert found == ["pull test-catalog parked 1 of 8 records (contract or handler)",
                     "pull protocols failed all 2 of its runs in staging"]
    assert reconcile.pull_problems(SOURCES, {}) == []  # not run in staging: not exercised


def test_the_staging_check_reads_pull_runs_for_the_designs_pull_sources():
    state = {"product_id": "EMR", "events": [{"node": "deploy_staging", "at": "2026-11-16T10:00:00+00:00"}]}

    class Both:
        def __init__(self):
            self.phrases = []

        def entries(self, **kw):
            self.phrases.append(kw["phrase"])
            return [pull_line("emr-labs", {"protocols": {"runs": 1, "failed": 1}})] if kw["phrase"] == PULLS else []

    logs = Both()
    check = reconcile.staging_check(services(logs), state, [], "staging", pulls=SOURCES)
    assert logs.phrases == [PULLS] and check["checked"]
    assert check["pulls"] == ["emr-labs/protocols", "emr-labs/test-catalog"]
    assert [p["dataset"] for p in check["problems"]] == ["protocols"]
    assert "emr-labs pull protocols failed all 1 of its runs" in reconcile.describe(check)
    ok = reconcile.staging_check(services(Logs()), state, [notes()], "staging",
                                 pulls={"acme-emr/emr-labs": ["test-catalog"]})
    assert reconcile.describe(ok) == "All 1 datasets and 1 pull sources reconcile in staging"


TRANSFORM_LINE = "valxel.transform.run"


def run_line(component, **rec):
    base = {"component": component, "published": True, "tests": {"passed": 9, "failed": 0}, "control_totals": 3,
            "classes_ok": True}
    return (component, f"{TRANSFORM_LINE} {json.dumps({**base, **rec})}")


def test_transform_runs_are_summed_and_a_failed_class_check_or_nothing_published_holds_the_release():
    rows = [run_line("emr-transform"), run_line("emr-transform", published=False, tests={"passed": 8, "failed": 1}),
            run_line("emr-marts", published=False), run_line("emr-marts", published=False, classes_ok=False),
            ("emr-transform", "something else")]
    runs = reconcile.transform_runs(rows)
    assert runs["emr-transform"] == {"runs": 2, "published": 1, "tests_passed": 9, "tests_failed": 1,
                                     "control_totals": 3, "classes_failed": 0}
    found = reconcile.transform_problems(["acme-emr/emr-transform", "acme-emr/emr-marts", "acme-emr/quiet"], runs)
    assert [(p["repo"], p["message"].split(":")[0]) for p in found] == [
        ("acme-emr/emr-marts", "1 run(s) failed the data-class check in staging"),
        ("acme-emr/emr-marts", "ran 2 time(s) in staging and never published"),
    ]


def test_the_staging_check_reports_each_transforms_tests_and_control_totals_for_g6():
    state = {"product_id": "EMR", "events": [{"node": "deploy_staging", "at": "2026-11-16T10:00:00+00:00"}]}

    class Runs:
        def entries(self, **kw):
            return [run_line("emr-transform")] if kw["phrase"] == TRANSFORM_LINE else []

    check = reconcile.staging_check(services(Runs()), state, [], "staging", transforms=["acme-emr/emr-transform"])
    assert check["checked"] and check["transforms"] == ["emr-transform"] and check["problems"] == []
    assert reconcile.describe(check) == ("All 1 transforms reconcile in staging; emr-transform published 1 of 1 "
                                         "runs, 9 tests and 3 control totals passed")
