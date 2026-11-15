"""Context measurement: how full each agent session's context gets, and where it goes.

Every ledger record carries its session (id and how it started), the model's window, the largest
prompt any one call in the session sent (the vendor's own count: input, cache writes and cache
reads) and an estimate of each prompt layer's size. The report groups them by week and role, so
the caps on prompt layers and the rule for resuming sessions are set from real numbers
(the design: "Valxel: agent context and session management").

    valxel usage context [--product P] [--weeks N] [--json]
    GET /api/usage/context?product_id=P&weeks=N
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from .gateway.prompt import PromptLayers, estimate_tokens

LAYERS = ("L1", "L2", "L3", "L4", "L5", "L6")
RESUME_FILL = 0.7  # above this a session hands off instead of resuming (the design's rule)


def layer_tokens(layers: PromptLayers) -> dict[str, int]:
    """Estimated tokens per prompt layer (about 4 characters per token; the vendor's count of the
    whole prompt is in the record beside it)."""
    sizes = {k: estimate_tokens(v) for k, v in layers.layers().items()}
    sizes["L6"] = estimate_tokens(layers.tail)
    return sizes


def week_of(ts: str) -> str:
    d = datetime.fromisoformat(ts)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, round(q * (len(s) - 1)))]


def report(records: Iterable, *, weeks: int = 4, now: datetime | None = None) -> list[dict]:
    """One row per ISO week, role and model, newest week first:

    calls, sessions (by mode), context window, fill (average, 90th percentile, largest, and how many
    sessions went over RESUME_FILL), average tokens per layer, cache-hit rate and cost.
    """
    now = now or datetime.now(UTC)
    since = (now - timedelta(weeks=weeks)).isoformat()
    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in records:
        if r.ts >= since:
            groups[(week_of(r.ts), r.role, r.model)].append(r)
    rows = []
    for (week, role, model), rs in groups.items():
        fills = [r.context_fill for r in rs if r.context_fill is not None]
        total_in = sum(r.total_input for r in rs)
        modes: dict[str, int] = defaultdict(int)
        seen: set[str] = set()
        for r in rs:
            key = r.session_id or f"call:{r.request_id}"
            if key not in seen:
                seen.add(key)
                modes[r.session_mode or "single"] += 1
        layer_avg = {k: round(sum((r.layer_tokens or {}).get(k, 0) for r in rs) / len(rs)) for k in LAYERS}
        rows.append({
            "week": week, "role": role, "model": model, "calls": len(rs), "sessions": dict(sorted(modes.items())),
            "context_window": max((r.context_window for r in rs), default=0),
            "fill_avg": round(sum(fills) / len(fills), 4) if fills else None,
            "fill_p90": round(_pct(fills, 0.9), 4) if fills else None,
            "fill_max": round(max(fills), 4) if fills else None,
            "over_resume_fill": sum(1 for f in fills if f > RESUME_FILL),
            "context_tokens_max": max((r.context_tokens for r in rs), default=0),
            "layer_tokens_avg": layer_avg,
            "cache_hit_rate": round(sum(r.cache_read_input_tokens for r in rs) / total_in, 4) if total_in else 0.0,
            "cost_usd": round(sum(r.est_cost_usd for r in rs), 4),
        })
    return sorted(rows, key=lambda x: (x["week"], x["cost_usd"]), reverse=True)


def table(rows: list[dict]) -> str:
    """The report as text for the terminal."""
    if not rows:
        return "No agent calls in this period."

    def pct(v):
        return "unknown" if v is None else f"{v:.0%}"

    head = f"{'week':<9} {'role':<16} {'model':<30} {'calls':>5} {'fill avg':>8} {'p90':>8} {'max':>8} " \
           f"{'>70%':>4} {'cache':>6} {'cost':>9}  largest layers"
    lines = [head]
    for r in rows:
        big = sorted(r["layer_tokens_avg"].items(), key=lambda kv: kv[1], reverse=True)[:3]
        layers = ", ".join(f"{k} {v:,}" for k, v in big if v)
        lines.append(f"{r['week']:<9} {r['role']:<16} {r['model'][:30]:<30} {r['calls']:>5} {pct(r['fill_avg']):>8} "
                     f"{pct(r['fill_p90']):>8} {pct(r['fill_max']):>8} {r['over_resume_fill']:>4} "
                     f"{r['cache_hit_rate']:>6.0%} {r['cost_usd']:>9.2f}  {layers}")
    return "\n".join(lines)
