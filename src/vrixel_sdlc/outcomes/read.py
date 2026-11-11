"""Reading a product's goals: check each goal's metric, read one for a window, record a number
someone typed in. The readouts, initiative status and review decisions build on these."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import DEFAULT_MIN_CELL, REGULATED, Metric, Reading, parse_metric
from .sources import default_sources, now


def sources(services) -> dict:
    if getattr(services, "readings", None) is None:
        from .store import InMemoryReadingStore

        services.readings = InMemoryReadingStore()
    if getattr(services, "metric_sources", None) is None:
        services.metric_sources = default_sources(services.readings)
    return services.metric_sources


def product_metrics(services, product_id: str) -> tuple[dict, str]:
    """The product's tools.metrics ({sources, min_cell}) and its data class."""
    entry = services.settings.entry(product_id)
    try:
        tools = services.snapshot(product_id).product.tools or {}
    except Exception:
        tools = {}
    cfg = tools.get("metrics") or {}
    return {"sources": dict(cfg.get("sources") or {}), "min_cell": int(cfg.get("min_cell") or DEFAULT_MIN_CELL)}, \
        entry.data_class


def goals(services, product_id: str) -> list[dict]:
    from ..roadmap import read_roadmap

    return read_roadmap(services, product_id)["goals"]


def check(services, product_id: str) -> list[dict]:
    """Every goal with its metric and what's wrong with it: [{goal_id, title, metric, problems}]."""
    cfg, data_class = product_metrics(services, product_id)
    srcs = sources(services)
    out = []
    for g in goals(services, product_id):
        metric, problems = parse_metric(g["id"], g.get("metric"))
        if metric is None and not problems:
            problems = [f"{g['id']}: no metric yet; readouts will say 'not measured'"]
        elif metric is not None:
            if metric.source != "manual" and metric.source not in cfg["sources"]:
                problems.append(f"{g['id']}: metric.source {metric.source} isn't in product.yaml tools.metrics "
                                f"(it has {', '.join(sorted(cfg['sources'])) or 'none'}; manual needs none)")
            problems += srcs[metric.source].check(metric, data_class)
        out.append({"goal_id": g["id"], "title": g.get("title", ""), "measure": g.get("measure", ""),
                    "metric": metric.as_dict() if metric else None, "problems": problems})
    return out


def _goal_metric(services, product_id: str, goal_id: str) -> Metric:
    for g in goals(services, product_id):
        if g["id"] == goal_id:
            metric, problems = parse_metric(goal_id, g.get("metric"))
            if problems:
                raise ValueError("; ".join(problems))
            if metric is None:
                raise ValueError(f"{goal_id} has no metric in roadmap/goals.yaml")
            return metric
    raise KeyError(f"No goal {goal_id} in {product_id}'s roadmap/goals.yaml")


def window(days: int, end: str = "") -> tuple[str, str]:
    t1 = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else datetime.now(timezone.utc)
    t1 = t1 if t1.tzinfo else t1.replace(tzinfo=timezone.utc)
    return (t1 - timedelta(days=days)).isoformat(), t1.isoformat()


def suppress(reading: Reading, data_class: str, min_cell: int) -> Reading:
    """Small counts never leave as numbers: the reading says 'under <min_cell>' instead."""
    if reading.status == "ok" and reading.n is not None and reading.n < min_cell:
        reading.status, reading.value, reading.n = "suppressed", None, None
        reading.note = f"fewer than {min_cell} behind it"
    elif reading.status == "ok" and reading.n is None and data_class in REGULATED and reading.source in ("sql", "posthog"):
        reading.status, reading.value = "error", None
        reading.note = "a regulated product's reading must say how many it counts (n)"
    return reading


def read(services, product_id: str, goal_id: str, *, start: str, end: str, record: bool = True) -> Reading:
    """One reading of a goal's metric for [start, end), suppressed and (by default) recorded."""
    metric = _goal_metric(services, product_id, goal_id)
    cfg, data_class = product_metrics(services, product_id)
    src = sources(services)[metric.source]
    problems = src.check(metric, data_class)
    if metric.source != "manual" and metric.source not in cfg["sources"]:
        problems.append(f"metric.source {metric.source} isn't in product.yaml tools.metrics")
    if problems:
        reading = Reading(product_id=product_id, goal_id=goal_id, source=metric.source, start=start, end=end,
                          status="error", note="; ".join(problems), at=now())
    else:
        try:
            reading = src.read(cfg["sources"].get(metric.source) or {}, metric, start, end, product_id=product_id)
        except Exception as exc:  # recorded as an error reading; the loop carries on
            reading = Reading(product_id=product_id, goal_id=goal_id, source=metric.source, start=start, end=end,
                              status="error", note=f"{type(exc).__name__}: {exc}"[:300], at=now())
    reading = suppress(reading, data_class, cfg["min_cell"])
    if record and metric.source != "manual":
        sources(services)
        services.readings.add(reading)
    return reading


def record_manual(services, product_id: str, goal_id: str, *, value: float, start: str, end: str,
                  entered_by: str, n: int | None = None) -> Reading:
    """A number someone typed in for a goal whose source is manual."""
    metric = _goal_metric(services, product_id, goal_id)
    if metric.source != "manual":
        raise ValueError(f"{goal_id} is read from {metric.source}; only manual goals take typed-in numbers")
    _, data_class = product_metrics(services, product_id)
    if data_class in REGULATED and n is None:
        raise ValueError(f"{product_id} holds {data_class} data: say how many the number counts (n)")
    if not entered_by:
        raise ValueError("Say who entered it")
    if datetime.fromisoformat(start.replace("Z", "+00:00")) >= datetime.fromisoformat(end.replace("Z", "+00:00")):
        raise ValueError("The period must end after it starts")
    reading = Reading(product_id=product_id, goal_id=goal_id, source="manual", start=start, end=end, status="ok",
                      value=float(value), n=n, at=now(), entered_by=entered_by, note="entered by hand")
    sources(services)
    services.readings.add(reading)
    return reading
