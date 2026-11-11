"""Outcomes, step 1: goal metrics the tool can read, the sources it reads them from, and the rules
that keep every reading an aggregate."""

from __future__ import annotations

import pytest
import yaml

from vrixel_sdlc.governance.manifest import parse_manifest
from vrixel_sdlc.outcomes import Metric, parse_metric, stub_from_measure
from vrixel_sdlc.outcomes import read as outcomes
from vrixel_sdlc.outcomes.sources import ManualSource, PostHogSource, PrometheusSource, SqlSource, select_problems

GOOD = ("SELECT avg(minutes) AS value, count(*) AS n FROM visits "
        "WHERE arrived_at >= :start AND arrived_at < :end")


def metric(query=GOOD, source="sql", **kw) -> Metric:
    return Metric(goal_id="GOAL-X-1", source=source, query=query, direction=kw.pop("direction", "down"), **kw)


# ---------------------------------------------------------------------------- definitions

def test_a_metric_is_parsed_and_its_mistakes_named():
    m, problems = parse_metric("GOAL-X-1", {"source": "sql", "query": GOOD, "unit": "minutes", "direction": "down",
                                            "baseline": 6.5, "target": 3, "by": "2027-06-30"})
    assert not problems and m.target == 3.0 and m.baseline == 6.5
    assert parse_metric("GOAL-X-1", None) == (None, [])
    _, problems = parse_metric("GOAL-X-1", {"source": "excel", "direction": "sideways", "by": "June", "colour": 1})
    text = " ".join(problems)
    assert "metric.source must be one of" in text and "up or down" in text and "metric.by" in text and "colour" in text
    _, problems = parse_metric("GOAL-X-1", {"source": "sql", "direction": "down"})
    assert problems == ["GOAL-X-1: metric.query is required for source sql"]
    _, problems = parse_metric("GOAL-X-1", {"source": "manual", "direction": "down", "baseline": 2, "target": 5})
    assert "wrong side of the baseline" in problems[0]


def test_a_brief_measure_becomes_a_manual_metric():
    assert stub_from_measure("under 2 a month per farm") == {"source": "manual", "unit": "a month per farm",
                                                             "direction": "down", "target": 2}
    assert stub_from_measure("15% less per hectare") == {"source": "manual", "unit": "% less per hectare",
                                                         "direction": "up", "target": 15}
    assert stub_from_measure("1,200 weekly active farms")["target"] == 1200
    assert stub_from_measure("Managers trust the alerts") == {"source": "manual", "unit": "", "direction": "up"}


# --------------------------------------------------------------------------- query shapes

@pytest.mark.parametrize("query, says", [
    (GOOD.replace("SELECT", "DELETE FROM visits; SELECT"), "one statement"),
    ("UPDATE visits SET x = 1 WHERE a >= :start AND a < :end", "must be a SELECT"),
    (GOOD.replace("FROM visits", "FROM visits, dblink('x')"), "may only read"),
    (GOOD.replace(":start", "now()"), ":start and :end"),
    ("SELECT minutes AS value, 1 AS n FROM visits WHERE a >= :start AND a < :end", "must aggregate"),
    (GOOD.replace("AS value", "AS v"), "`value`"),
    (GOOD + " GROUP BY patient_id", "one row"),
    (GOOD.replace(", count(*) AS n", ""), "must also return `n`"),
])
def test_queries_that_could_leak_rows_are_refused(query, says):
    problems = select_problems(metric(query), "phi")
    assert any(says in p for p in problems), problems


def test_a_subquery_may_group_and_strings_are_not_keywords():
    q = ("SELECT avg(c) AS value, count(*) AS n FROM (SELECT clinic, count(*) AS c FROM visits "
         "WHERE kind = 'update' AND arrived_at >= :start AND arrived_at < :end GROUP BY clinic) t")
    assert select_problems(metric(q), "phi") == []
    assert select_problems(metric(GOOD.replace(", count(*) AS n", "")), "general") == []


class FakeCursor:
    def __init__(self, columns, rows, log):
        self.description = [(c,) for c in columns]
        self.rows, self.log = rows, log

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchmany(self, n):
        return self.rows[:n]


class FakeConn:
    def __init__(self, columns, rows, log):
        self.c = FakeCursor(columns, rows, log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.c


def sql_source(columns, rows, log=None):
    log = [] if log is None else log
    return SqlSource(connect=lambda dsn: FakeConn(columns, rows, log)), log


def test_sql_reads_one_aggregate_read_only():
    src, log = sql_source(["value", "n"], [(4.2, 40)])
    r = src.read({}, metric(), "2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00", product_id="CLN")
    assert (r.status, r.value, r.n) == ("ok", 4.2, 40)
    assert log[0][0] == "SET TRANSACTION READ ONLY" and "statement_timeout" in log[1][0]
    assert "%(start)s" in log[2][0] and log[2][1]["end"].startswith("2026-11-01")
    src, _ = sql_source(["value", "patient_id"], [(1, "p-1")])
    assert "only value and n" in src.read({}, metric(), "2026-10-01", "2026-11-01", product_id="CLN").note
    src, _ = sql_source(["value", "n"], [(1, 20), (2, 20)])
    assert src.read({}, metric(), "2026-10-01", "2026-11-01", product_id="CLN").status == "error"
    src, _ = sql_source(["value", "n"], [(None, 0)])
    assert src.read({}, metric(), "2026-10-01", "2026-11-01", product_id="CLN").status == "no_data"


def test_prometheus_answers_with_one_series():
    src = PrometheusSource()
    assert src.check(metric("sum(increase(vrixel_alerts_sent_total[$window]))", "prometheus"), "phi") == []
    assert src.check(metric("sum by (farm) (vrixel_alerts_sent_total)", "prometheus"), "general")
    assert src.check(metric("rate(vrixel_alerts_sent_total[5m])", "prometheus"), "general")
    assert src.check(metric("sum(vrixel_logins_total{user_id='1'})", "prometheus"), "general")
    seen = {}

    def http(url, params, headers):
        seen.update(params, url=url)
        return {"data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "37"]}]}}

    r = PrometheusSource(http).read({"url": "https://prom.example"}, metric("sum(increase(x[$window]))", "prometheus"),
                                    "2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00", product_id="FARM")
    assert r.value == 37.0 and seen["query"] == "sum(increase(x[2592000s]))" and seen["url"].endswith("/api/v1/query")
    two = PrometheusSource(lambda *a: {"data": {"resultType": "vector", "result": [{"value": [0, "1"]}] * 2}})
    assert two.read({"url": "https://p"}, metric("sum(x)", "prometheus"), "2026-10-01", "2026-10-31",
                    product_id="FARM").status == "error"


def test_posthog_runs_hogql_with_the_window():
    sent = {}

    def http(url, body, headers):
        sent.update(url=url, q=body["query"]["query"])
        return {"columns": ["value", "n"], "results": [[0.42, 300]]}

    q = "SELECT countIf(event = 'alert_opened') / count() AS value, count() AS n FROM events WHERE timestamp >= :start AND timestamp < :end"
    r = PostHogSource(http).read({"url": "https://ph.example", "project": "12"}, metric(q, "posthog"),
                                 "2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00", product_id="FARM")
    assert r.value == 0.42 and sent["url"] == "https://ph.example/api/projects/12/query/"
    assert "toDateTime('2026-10-01 00:00:00')" in sent["q"]


# ------------------------------------------------------------------------- product.yaml

EXAMPLES = {"CLN": "examples/governance/acme-clinic/cln-governance/product.yaml",
            "FARM": "examples/governance/acme-farm/farm-governance/product.yaml"}


def manifest(services, metrics, pid="CLN"):
    from pathlib import Path

    s = services.settings
    raw = yaml.safe_load((Path(__file__).parent.parent / EXAMPLES[pid]).read_text())
    raw.setdefault("tools", {})["metrics"] = metrics
    try:
        product, _ = parse_manifest(yaml.safe_dump(raw), s.entry(pid), s.limits, s.registry, s.tools, s.quality, s.repos)
        return product.tools.get("metrics"), ""
    except Exception as exc:
        return None, str(exc)


def test_product_metric_sources_are_checked(services):
    got, err = manifest(services, {"sql": {}, "prometheus": {"url": "https://prom.cln.example/"}})
    assert not err and got == {"sources": {"sql": {}, "prometheus": {"url": "https://prom.cln.example"}}, "min_cell": 11}
    assert "not approved for phi" in manifest(services, {"posthog": {"url": "https://ph", "project": 1}})[1]
    assert "platform secrets" in manifest(services, {"sql": {"dsn": "postgres://x"}})[1]
    assert "at least 11" in manifest(services, {"sql": {}, "min_cell": 5})[1]
    got, err = manifest(services, {"posthog": {"url": "https://ph.example", "project": 7}}, pid="FARM")
    assert not err and got["min_cell"] == 1  # suppression is off outside PHI unless the product sets it
    got, _ = manifest(services, {"sql": {}, "min_cell": 5}, pid="FARM")
    assert got["min_cell"] == 5


# ------------------------------------------------------------------------- reading goals

def test_the_sample_goals_check_clean(services):
    rows = {r["goal_id"]: r for r in outcomes.check(services, "CLN")}
    assert rows["GOAL-CLN-1"]["problems"] == [] and rows["GOAL-CLN-1"]["metric"]["source"] == "sql"
    assert rows["GOAL-CLN-2"]["metric"]["source"] == "manual"


def test_small_counts_never_leave_as_numbers(services):
    outcomes.sources(services)
    services.metric_sources["sql"], _ = sql_source(["value", "n"], [(4.0, 6)])
    r = outcomes.read(services, "CLN", "GOAL-CLN-1", start="2026-10-01", end="2026-11-01")
    assert (r.status, r.value, r.n) == ("suppressed", None, None) and r.shown() == "under 11"
    services.metric_sources["sql"], _ = sql_source(["value", "n"], [(4.0, 60)])
    r = outcomes.read(services, "CLN", "GOAL-CLN-1", start="2026-10-01", end="2026-11-01")
    assert r.status == "ok" and r.shown("minutes") == "4 minutes"
    assert [h["status"] for h in services.readings.history("CLN", "GOAL-CLN-1")] == ["suppressed", "ok"]
    services.metric_sources["sql"], _ = sql_source(["value", "n"], [(0.0, 0)])
    assert outcomes.read(services, "CLN", "GOAL-CLN-1", start="2026-10-01", end="2026-11-01").shown() == "0"
    services.metric_sources["sql"], _ = sql_source(["value"], [(4.0,)])
    assert outcomes.read(services, "CLN", "GOAL-CLN-1", start="2026-10-01", end="2026-11-01").status == "error"


def test_a_number_typed_in_is_read_back(services):
    with pytest.raises(ValueError, match="how many"):
        outcomes.record_manual(services, "CLN", "GOAL-CLN-2", value=3.1, start="2026-10-01", end="2026-11-01",
                               entered_by="sunil")
    with pytest.raises(ValueError, match="read from sql"):
        outcomes.record_manual(services, "CLN", "GOAL-CLN-1", value=3.1, n=80, start="2026-10-01", end="2026-11-01",
                               entered_by="sunil")
    outcomes.record_manual(services, "CLN", "GOAL-CLN-2", value=3.1, n=812, start="2026-10-01T00:00:00+00:00",
                           end="2026-11-01T00:00:00+00:00", entered_by="sunil")
    r = outcomes.read(services, "CLN", "GOAL-CLN-2", start="2026-10-15T00:00:00+00:00", end="2026-11-15T00:00:00+00:00")
    assert (r.status, r.value, r.entered_by) == ("ok", 3.1, "sunil")
    r = outcomes.read(services, "CLN", "GOAL-CLN-2", start="2026-11-02T00:00:00+00:00", end="2026-12-01T00:00:00+00:00")
    assert r.status == "no_data" and isinstance(services.metric_sources["manual"], ManualSource)


def test_inception_writes_measurable_goals(services):
    from vrixel_sdlc.inception import Vision
    from vrixel_sdlc.inception import setup
    from vrixel_sdlc.inception.run import run

    v = Vision(name="Aqua Watch", org="acme-water", vision="Alert farm managers when fields run dry.",
               goals=["Fewer dry fields (measure: under 2 a month per farm)"], users=["Farm managers"], budget_usd=1500)
    [b] = run(services, v)
    text, _ = setup.goals_yaml(b.text)
    goals = yaml.safe_load(text)["goals"]
    assert goals and all(parse_metric(g["id"], g["metric"])[1] == [] for g in goals)
    assert goals[0]["metric"]["source"] == "manual"


def test_the_command_checks_and_reads(services, capsys):
    from argparse import Namespace

    from vrixel_sdlc.cli import _outcomes

    base = dict(product="CLN", goal="", days=30, start="", end="", value=None, n=None, by="sunil", dry_run=True,
                json=False)
    assert _outcomes(Namespace(action="check", **base), services) == 0
    assert "GOAL-CLN-1" in capsys.readouterr().out
    assert _outcomes(Namespace(action="read", **{**base, "goal": "GOAL-CLN-1"}), services) == 0
    assert "minutes" in capsys.readouterr().out
    assert _outcomes(Namespace(action="read", **{**base, "goal": "GOAL-CLN-9"}), services) == 2
