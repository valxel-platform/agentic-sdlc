"""Where a goal's numbers come from. Each source checks a metric's query when it is defined and reads
one aggregate for a window; none of them ever hands the tool rows about people.

    sql          the product's own database, through a read-only role on its read replica. The
                 query is one SELECT with :start and :end, returning one row: `value`, and `n`
                 (the count behind it; required for PHI and payments products). Runs read-only
                 with a statement timeout. DSN: platform secret VRIXEL_METRICS_DSN_<PRODUCT_ID>.
    prometheus   the product's Prometheus (behaviour events as OpenTelemetry counters, through the
                 collector the product already runs). One aggregation, no `by`/`without`, so the
                 answer is one series; `$window` becomes the window's length. Evaluated at the
                 window's end. Optional bearer token VRIXEL_PROMETHEUS_TOKEN_<PRODUCT_ID>.
    posthog      optional, for teams that already use PostHog: one HogQL SELECT, same rules as sql.
                 Key VRIXEL_POSTHOG_KEY_<PRODUCT_ID>; project id in product.yaml.
    manual       typed in by the owner (`vrixel outcomes record`, later a decision on the web):
                 the reading that covers the window.

The shape rules are the privacy boundary: a query that could return per-person rows is refused
when it is defined, and a result with more than one row, or a column other than value and n, is
refused when it is read.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Protocol

from . import REGULATED, Metric, Reading

SECRET_PREFIX = {"sql": "VRIXEL_METRICS_DSN_", "prometheus": "VRIXEL_PROMETHEUS_TOKEN_",
                 "posthog": "VRIXEL_POSTHOG_KEY_"}
TIMEOUT_S = 15
_WRITE = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|grant|revoke|truncate|copy|call|do|"
                    r"execute|vacuum|refresh|lock|set|reset|pg_sleep|dblink|lo_import|lo_export)\b", re.I)
_AGG = re.compile(r"\b(count|sum|avg|min|max|percentile_cont|percentile_disc|median|quantile\w*|uniq\w*|"
                  r"stddev\w*)\s*\(", re.I)
_PROM_AGG = re.compile(r"^\s*(sum|avg|min|max|count|quantile|histogram_quantile|scalar)\s*\(", re.I)
ALLOWED_COLUMNS = {"value", "n"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetricSource(Protocol):
    provider: str

    def check(self, metric: Metric, data_class: str) -> list[str]: ...
    def read(self, cfg: dict, metric: Metric, start: str, end: str, *, product_id: str) -> Reading: ...


def _reading(product_id: str, metric: Metric, start: str, end: str, status: str, **kw) -> Reading:
    return Reading(product_id=product_id, goal_id=metric.goal_id, source=metric.source, start=start, end=end,
                   status=status, at=now(), **kw)


def secret(provider: str, product_id: str) -> str:
    return os.environ.get(SECRET_PREFIX.get(provider, "") + product_id.upper(), "") if provider in SECRET_PREFIX else ""


# ------------------------------------------------------------------------------- SQL-like checks

def select_problems(metric: Metric, data_class: str, *, dialect: str = "SQL") -> list[str]:
    """One read-only SELECT with :start and :end, aggregating to `value` (and `n` when regulated)."""
    gid, q = metric.goal_id, metric.query.strip().rstrip(";").strip()
    out = []
    if ";" in q:
        out.append(f"{gid}: the {dialect} query must be one statement")
    if not re.match(r"^(select|with)\b", q, re.I):
        out.append(f"{gid}: the {dialect} query must be a SELECT")
    bad = sorted({m.group(1).lower() for m in _WRITE.finditer(_strip_strings(q))})
    if bad:
        out.append(f"{gid}: the {dialect} query may only read; it uses {', '.join(bad)}")
    if ":start" not in q or ":end" not in q:
        out.append(f"{gid}: the {dialect} query must filter on the window with :start and :end")
    if not _AGG.search(q):
        out.append(f"{gid}: the {dialect} query must aggregate (count, sum, avg, a percentile...): "
                   "the tool reads one number, never rows")
    if not re.search(r"\bas\s+value\b", q, re.I):
        out.append(f"{gid}: the {dialect} query must name its result column `value` (… AS value)")
    if re.search(r"\bgroup\s+by\b", _outer(q), re.I):
        out.append(f"{gid}: the {dialect} query must return one row; GROUP BY belongs in a subquery")
    if data_class in REGULATED and not re.search(r"\bas\s+n\b", q, re.I):
        out.append(f"{gid}: a {data_class} product's query must also return `n`, the count behind the value "
                   "(… count(*) AS n), so small counts can be suppressed")
    return out


def _strip_strings(q: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", q)


def _outer(q: str) -> str:
    """The query without its parenthesised parts, so a subquery's GROUP BY is allowed."""
    prev = None
    q = _strip_strings(q)
    while prev != q:
        prev, q = q, re.sub(r"\([^()]*\)", " _ ", q)
    return q


def one_row(product_id: str, metric: Metric, start: str, end: str, columns: list[str], rows: list) -> Reading:
    cols = [str(c).lower() for c in columns]
    extra = sorted(set(cols) - ALLOWED_COLUMNS)
    if extra or "value" not in cols:
        return _reading(product_id, metric, start, end, "error",
                        note=f"the query must return only value and n; it returned {', '.join(cols) or 'nothing'}")
    if len(rows) != 1:
        return _reading(product_id, metric, start, end, "error",
                        note=f"the query must return one row; it returned {len(rows)}")
    row = dict(zip(cols, rows[0]))
    value = row.get("value")
    n = row.get("n")
    if value is None:
        return _reading(product_id, metric, start, end, "no_data", n=int(n) if n is not None else None,
                        note="nothing in this window")
    return _reading(product_id, metric, start, end, "ok", value=float(value), n=int(n) if n is not None else None)


# ------------------------------------------------------------------------------------- sources

class SqlSource:
    provider = "sql"

    def __init__(self, connect=None) -> None:
        self._connect = connect  # tests pass a fake; live uses psycopg

    def check(self, metric: Metric, data_class: str) -> list[str]:
        return select_problems(metric, data_class)

    def read(self, cfg: dict, metric: Metric, start: str, end: str, *, product_id: str) -> Reading:
        dsn = cfg.get("dsn") or secret("sql", product_id)
        if not dsn and self._connect is None:
            return _reading(product_id, metric, start, end, "error",
                            note=f"no database to read: set {SECRET_PREFIX['sql']}{product_id.upper()}")
        sql = re.sub(r":(start|end)\b", r"%(\1)s", metric.query.strip().rstrip(";"))
        connect = self._connect or _psycopg_connect
        with connect(dsn) as conn:
            cur = conn.cursor()
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {TIMEOUT_S * 1000}")
            cur.execute(sql, {"start": start, "end": end})
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(2)
        return one_row(product_id, metric, start, end, columns, rows)


def _psycopg_connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=False, connect_timeout=TIMEOUT_S)


class PrometheusSource:
    provider = "prometheus"

    def __init__(self, http=None) -> None:
        self._http = http  # callable(url, params, headers) -> json

    def check(self, metric: Metric, data_class: str) -> list[str]:
        gid, q = metric.goal_id, metric.query.strip()
        out = []
        if not _PROM_AGG.match(q):
            out.append(f"{gid}: the PromQL query must be one aggregation (sum, avg, min, max, count, quantile, "
                       "histogram_quantile or scalar) so it answers with one number")
        if re.search(r"\b(by|without)\s*\(", q, re.I) and not re.match(r"^\s*histogram_quantile\s*\(", q, re.I):
            out.append(f"{gid}: the PromQL query must not group with by or without; that answers per label")
        if re.search(r"\b(user|patient|account|member|customer|email)(_id)?\b", q, re.I):
            out.append(f"{gid}: the PromQL query names a person-level label; metrics stay aggregate")
        return out

    def read(self, cfg: dict, metric: Metric, start: str, end: str, *, product_id: str) -> Reading:
        url = str(cfg.get("url") or "").rstrip("/")
        if not url:
            return _reading(product_id, metric, start, end, "error", note="tools.metrics.prometheus.url isn't set")
        t0, t1 = _ts(start), _ts(end)
        query = metric.query.replace("$window", f"{max(1, int(t1 - t0))}s")
        headers = {"Authorization": f"Bearer {secret('prometheus', product_id)}"} if secret("prometheus", product_id) else {}
        body = (self._http or _http_get)(f"{url}/api/v1/query", {"query": query, "time": f"{t1:.0f}"}, headers)
        data = (body or {}).get("data") or {}
        result = data.get("result")
        if data.get("resultType") == "scalar":
            return _reading(product_id, metric, start, end, "ok", value=float(result[1]))
        if not result:
            return _reading(product_id, metric, start, end, "no_data", note="no series in this window")
        if len(result) != 1:
            return _reading(product_id, metric, start, end, "error",
                            note=f"the query must answer with one series; it answered with {len(result)}")
        value = float(result[0]["value"][1])
        if value != value:  # NaN: nothing happened in the window
            return _reading(product_id, metric, start, end, "no_data", note="nothing in this window")
        return _reading(product_id, metric, start, end, "ok", value=value)


def _ts(iso: str) -> float:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def _http_get(url: str, params: dict, headers: dict) -> dict:
    import httpx

    r = httpx.get(url, params=params, headers=headers, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _http_post(url: str, body: dict, headers: dict) -> dict:
    import httpx

    r = httpx.post(url, json=body, headers=headers, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


class PostHogSource:
    provider = "posthog"

    def __init__(self, http=None) -> None:
        self._http = http  # callable(url, body, headers) -> json

    def check(self, metric: Metric, data_class: str) -> list[str]:
        return select_problems(metric, data_class, dialect="HogQL")

    def read(self, cfg: dict, metric: Metric, start: str, end: str, *, product_id: str) -> Reading:
        url, project = str(cfg.get("url") or "").rstrip("/"), str(cfg.get("project") or "")
        key = secret("posthog", product_id)
        if not url or not project:
            return _reading(product_id, metric, start, end, "error", note="tools.metrics.posthog needs url and project")
        q = metric.query.strip().rstrip(";")
        q = q.replace(":start", f"toDateTime('{_hog_time(start)}')").replace(":end", f"toDateTime('{_hog_time(end)}')")
        body = (self._http or _http_post)(f"{url}/api/projects/{project}/query/",
                                          {"query": {"kind": "HogQLQuery", "query": q}},
                                          {"Authorization": f"Bearer {key}"} if key else {})
        return one_row(product_id, metric, start, end, list(body.get("columns") or []), list(body.get("results") or []))


def _hog_time(iso: str) -> str:
    t = datetime.fromtimestamp(_ts(iso), tz=timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S")


class ManualSource:
    """Readings people typed in: the latest one whose period ends inside the window."""
    provider = "manual"

    def __init__(self, store) -> None:
        self.store = store

    def check(self, metric: Metric, data_class: str) -> list[str]:
        return []

    def read(self, cfg: dict, metric: Metric, start: str, end: str, *, product_id: str) -> Reading:
        t0, t1 = _ts(start), _ts(end)
        for r in reversed(self.store.history(product_id, metric.goal_id)):
            if r.get("source") == "manual" and r.get("status") == "ok" and t0 < _ts(r["end"]) <= t1:
                return _reading(product_id, metric, r["start"], r["end"], "ok", value=r["value"], n=r.get("n"),
                                entered_by=r.get("entered_by", ""), note="entered by hand")
        return _reading(product_id, metric, start, end, "no_data", note="waiting for someone to enter it")


class StubSource:
    """Stub delivery: a plausible number between the baseline and the target, so the demo and tests
    have readings without a database. Labelled as a stub on every reading."""

    def __init__(self, provider: str, real) -> None:
        self.provider = provider
        self.real = real

    def check(self, metric: Metric, data_class: str) -> list[str]:
        return self.real.check(metric, data_class)

    def read(self, cfg: dict, metric: Metric, start: str, end: str, *, product_id: str) -> Reading:
        lo = metric.baseline if metric.baseline is not None else (metric.target or 0) * (1.5 if metric.direction == "down" else 0.5)
        hi = metric.target if metric.target is not None else lo
        return _reading(product_id, metric, start, end, "ok", value=round(lo + (hi - lo) * 0.4, 2), n=120,
                        note="stub reading")


def default_sources(store, *, live: bool | None = None) -> dict[str, MetricSource]:
    if live is None:
        live = os.environ.get("VRIXEL_DELIVERY", "stub").lower() == "live"
    real = {"sql": SqlSource(), "prometheus": PrometheusSource(), "posthog": PostHogSource()}
    out: dict[str, MetricSource] = {k: (v if live else StubSource(k, v)) for k, v in real.items()}
    out["manual"] = ManualSource(store)
    return out
