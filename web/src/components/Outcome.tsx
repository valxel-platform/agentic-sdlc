import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type GoalMetric, type GoalOutcome, type OutcomeStatus, type Readout, type WantedNumber } from "../api";
import { threadPath } from "../vocab";

const STATUS: Record<OutcomeStatus, { label: string; chip: string }> = {
  on_track: { label: "On track", chip: "go" },
  flat: { label: "Flat", chip: "wait" },
  off_track: { label: "Off track", chip: "stop" },
  no_data: { label: "No data", chip: "plain" },
};

const SOURCE: Record<string, string> = {
  sql: "read from the product's database", prometheus: "read from the product's metrics",
  posthog: "read from PostHog", manual: "entered by hand",
};

export function num(v: number | null | undefined, unit = ""): string {
  if (v === null || v === undefined) return "no data";
  const n = Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
  if (!unit) return n;
  return unit.startsWith("%") ? `${n}${unit}` : `${n} ${unit}`;
}

export function StatusChip({ status, why }: { status: OutcomeStatus; why?: string }) {
  const s = STATUS[status] ?? STATUS.no_data;
  return <span className={`chip ${s.chip}`} title={why}>{s.label}</span>;
}

/** The goal's readings over time, with its target as a dashed line. */
function Trend({ points, metric }: { points: GoalOutcome["trend"]; metric: GoalMetric }) {
  const vals = points.map((p) => p.value).filter((v): v is number => v !== null);
  if (vals.length < 2) return null;
  const all = [...vals, ...(metric.target !== null ? [metric.target] : []), ...(metric.baseline !== null ? [metric.baseline] : [])];
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const W = 132, H = 34, pad = 3;
  const x = (i: number) => pad + (i / (points.length - 1)) * (W - 2 * pad);
  const y = (v: number) => H - pad - ((v - lo) / span) * (H - 2 * pad);
  const line = points.map((p, i) => (p.value === null ? null : `${x(i)},${y(p.value)}`)).filter(Boolean).join(" ");
  return (
    <svg className="trend" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Trend: ${vals.map((v) => num(v)).join(", ")}${metric.target !== null ? `; target ${num(metric.target)}` : ""}`}>
      {metric.target !== null && <line x1={pad} x2={W - pad} y1={y(metric.target)} y2={y(metric.target)} className="trend-target" />}
      <polyline points={line} className="trend-line" />
      {vals.length > 0 && <circle cx={x(points.length - 1)} cy={y(vals[vals.length - 1])} r="2.5" className="trend-dot" />}
    </svg>
  );
}

export function GoalOutcomeLine({ o }: { o: GoalOutcome }) {
  const m = o.metric;
  return (
    <div className="goal-outcome">
      <StatusChip status={o.status} why={o.why} />
      <span className="small">
        {o.latest !== null ? <strong>{num(o.latest, m.unit)}</strong> : <span className="muted">No reading yet</span>}
        {m.baseline !== null && <span className="muted"> from {num(m.baseline, m.unit)}</span>}
        {m.target !== null && <span className="muted">, target {num(m.target, m.unit)}{m.by ? ` by ${m.by}` : ""}</span>}
      </span>
      <Trend points={o.trend} metric={m} />
      <span className="small faint">{SOURCE[m.source] ?? m.source}{o.status === "no_data" && o.why ? `; ${o.why}` : ""}</span>
    </div>
  );
}

function day(iso: string): string {
  return iso.slice(0, 10);
}

/** A number the product's owners are asked to enter for a hand-entered goal's readout. */
export function WantedForm({ productId, w, onDone }: { productId: string; w: WantedNumber; onDone: () => void }) {
  const [value, setValue] = useState("");
  const [n, setN] = useState("");
  const [k, setK] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    api.recordReading(productId, w.goal_id, {
      value: Number(value), start: w.start, end: w.end,
      ...(n ? { n: Number(n) } : {}), ...(k ? { k: Number(k) } : {}),
    }).then(() => { setErr(""); onDone(); }).catch((x) => setErr(x.message)).finally(() => setBusy(false));
  };
  return (
    <li>
      <form className="wanted" onSubmit={submit}>
        <div>
          <strong>{w.goal_id}</strong>{" "}
          <span className="small muted">
            {day(w.start)} to {day(w.end)}, {w.window_days} days after <Link to={threadPath(w.feature_id)}>{w.feature_id}</Link> reached everyone
          </span>
        </div>
        <label className="small">Value{w.unit ? ` (${w.unit})` : ""}
          <input required inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        <label className="small">Counted (n)
          <input inputMode="numeric" value={n} onChange={(e) => setN(e.target.value)} placeholder="Optional" />
        </label>
        <label className="small">Of those, on top (k)
          <input inputMode="numeric" value={k} onChange={(e) => setK(e.target.value)} placeholder="For a rate" />
        </label>
        <button className="btn" disabled={busy || !value}>Save number</button>
        {err && <p className="error-note" role="alert">{err}</p>}
      </form>
    </li>
  );
}

export function ReadoutTable({ readouts, metrics }: { readouts: Readout[]; metrics?: Record<string, GoalMetric> }) {
  if (readouts.length === 0) return null;
  return (
    <table className="quality">
      <thead>
        <tr><th scope="col">Goal</th><th scope="col">Window</th><th scope="col">Before</th><th scope="col">After</th><th scope="col">Change</th></tr>
      </thead>
      <tbody>
        {readouts.map((r) => {
          const unit = metrics?.[r.goal_id]?.unit ?? r.unit;
          return (
            <tr key={`${r.goal_id}-${r.window_days}`}>
              <td>{r.goal_id}</td>
              <td>{r.window_days} days</td>
              <td>{r.before.status === "ok" ? num(r.before.value, unit) : r.before.status === "suppressed" ? "under the minimum" : "no data"}</td>
              <td>{r.after.status === "ok" ? num(r.after.value, unit) : r.after.status === "suppressed" ? "under the minimum" :
                r.source === "manual" ? "waiting for the number" : "no data"}</td>
              <td>
                {r.change === null ? <span className="muted">none yet</span> : (
                  <span className={`chip ${r.toward === true ? "go" : r.toward === false ? "stop" : "plain"}`}>
                    {r.change > 0 ? "+" : ""}{num(r.change, unit)}{r.toward === true ? ", toward target" : r.toward === false ? ", away" : ""}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
