"""Outcomes: whether a product's goals move. Step 1 of the outcome loop: each goal's metric, defined so
the tool can read it, and the sources it reads from.

A goal in roadmap/goals.yaml keeps its human sentence (`measure`) and gains a `metric`:

    goals:
      - id: GOAL-CLN-1
        title: Clinics check patients in faster
        measure: Median front-desk check-in time under 3 minutes
        metric:
          source: sql                 # sql | prometheus | posthog | manual
          query: |
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY minutes) AS value, count(*) AS n
            FROM visits WHERE arrived_at >= :start AND arrived_at < :end
          unit: minutes
          direction: down             # which way is better: up or down
          baseline: 6.5               # where it stood before the work (optional until known)
          target: 3
          by: 2027-06-30

The product names the sources it has in product.yaml (`tools.metrics`); `manual` needs none: the
owner types the number in when asked. The tool only ever holds aggregates: a query returns one row
(`value`, and `n`, the count behind it), and a count under the product's minimum cell size is shown
as "under <n>", never as the number. See outcomes/sources.py for the per-source rules.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

SOURCES = ("sql", "prometheus", "posthog", "manual")
DIRECTIONS = ("up", "down")
STATUSES = ("ok", "suppressed", "no_data", "error")
DEFAULT_MIN_CELL = 11
REGULATED = ("phi", "payments")
# Health data follows the CMS cell-size convention: no count from 1 to 10 is shown (zero is). Other
# classes have no such rule (PCI protects card data, which never reaches a metric), so suppression is
# off unless the product sets tools.metrics.min_cell.
SUPPRESSED_CLASSES = ("phi",)


def min_cell_for(data_class: str) -> int:
    """The default and the floor for tools.metrics.min_cell: 11 for PHI, 1 (off) otherwise."""
    return DEFAULT_MIN_CELL if data_class in SUPPRESSED_CLASSES else 1


@dataclass(frozen=True)
class Metric:
    goal_id: str
    source: str
    query: str = ""
    unit: str = ""
    direction: str = "up"
    baseline: float | None = None
    target: float | None = None
    by: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reading:
    product_id: str
    goal_id: str
    source: str
    start: str
    end: str
    status: str  # ok | suppressed | no_data | error
    value: float | None = None
    n: int | None = None
    note: str = ""
    at: str = ""
    entered_by: str = ""  # manual readings: who typed it in
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    def shown(self, unit: str = "", min_cell: int = DEFAULT_MIN_CELL) -> str:
        """The reading as people see it."""
        if self.status == "suppressed" and min_cell > 1:
            return f"under {min_cell}"
        if self.status != "ok" or self.value is None:
            return "no data" + (f" ({self.note})" if self.note else "")
        v = int(self.value) if float(self.value).is_integer() else round(self.value, 2)
        return f"{v}{' ' + unit if unit else ''}"


def _num(value, key: str, problems: list[str], gid: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        problems.append(f"{gid}: metric.{key} must be a number, not {value!r}")
        return None


def parse_metric(goal_id: str, raw) -> tuple[Metric | None, list[str]]:
    """A goal's `metric` mapping, with what's wrong with it. No metric is not a problem here: the
    goal is just not measurable yet (readouts say so)."""
    if raw is None:
        return None, []
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, [f"{goal_id}: metric must be a mapping (source, query, unit, direction, baseline, target, by)"]
    source = str(raw.get("source") or "").strip().lower()
    if source not in SOURCES:
        problems.append(f"{goal_id}: metric.source must be one of {', '.join(SOURCES)}")
    direction = str(raw.get("direction") or "").strip().lower()
    if direction not in DIRECTIONS:
        problems.append(f"{goal_id}: metric.direction must be up or down (which way is better)")
    query = str(raw.get("query") or "").strip()
    if source and source != "manual" and not query:
        problems.append(f"{goal_id}: metric.query is required for source {source}")
    by = str(raw.get("by") or "").strip()
    if by:
        try:
            date.fromisoformat(by)
        except ValueError:
            problems.append(f"{goal_id}: metric.by must be a date (YYYY-MM-DD), not {by!r}")
    unknown = set(raw) - {"source", "query", "unit", "direction", "baseline", "target", "by"}
    if unknown:
        problems.append(f"{goal_id}: metric has unknown keys {sorted(unknown)}")
    baseline = _num(raw.get("baseline"), "baseline", problems, goal_id)
    target = _num(raw.get("target"), "target", problems, goal_id)
    if baseline is not None and target is not None and direction in DIRECTIONS:
        if (direction == "up" and target < baseline) or (direction == "down" and target > baseline):
            problems.append(f"{goal_id}: metric.target {target:g} is the wrong side of the baseline {baseline:g} "
                            f"for direction {direction}")
    if problems:
        return None, problems
    return Metric(goal_id=goal_id, source=source, query=query, unit=str(raw.get("unit") or ""), direction=direction,
                  baseline=baseline, target=target, by=by), []


_DOWN = re.compile(r"\b(under|below|less|fewer|lower|reduce[sd]?|cut|down|decrease[sd]?|at most|no more than|"
                   r"shorter|faster)\b", re.I)
_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)\s*(%|percent)?")


def stub_from_measure(measure: str) -> dict:
    """A starting `metric` for a goal written from a brief's measure sentence: entered by hand until
    someone wires a query. 'under 2 a month per farm' -> down, target 2, unit 'a month per farm'."""
    text = (measure or "").strip()
    m = _NUMBER.search(text)
    target = None
    unit = ""
    direction = "down" if _DOWN.search(text) else "up"
    if m:
        target = float(m.group(1).replace(",", ""))
        rest = text[m.end():].strip(" .,;:)")
        unit = rest
        if m.group(2):
            unit = f"% {rest}".strip()
            # '15% less water per hectare': the reading is the change itself, so more is better.
            if re.match(r"(less|fewer|lower|more|higher|faster|shorter|reduction|increase)\b", rest, re.I):
                direction = "up"
    out = {"source": "manual", "unit": unit[:60], "direction": direction}
    if target is not None:
        out["target"] = int(target) if target.is_integer() else target
    return out
