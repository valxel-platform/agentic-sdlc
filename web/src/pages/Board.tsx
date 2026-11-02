import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type FeatureSummary, type Me } from "../api";
import { useLive } from "../live";
import { Rail } from "../components/Rail";
import { RISK, ago, money, waitingLabel } from "../vocab";

function Status({ f }: { f: FeatureSummary }) {
  if (f.error) return <span className="chip stop">Failed: see details</span>;
  if (f.status === "done") return <span className="chip go">Released</span>;
  if (f.status === "aborted") return <span className="chip stop">Stopped</span>;
  const p = f.pending[0];
  if (p) return <span className={`chip ${p.type === "escalation" ? "stop" : p.type === "external" ? "run" : "wait"}`}>{waitingLabel(p, f.product_id)}</span>;
  return <span className="chip run">Agents working</span>;
}

export function Board({ me }: { me: Me }) {
  const [rows, setRows] = useState<FeatureSummary[] | null>(null);
  const [lane, setLane] = useState("");
  const [needsMe, setNeedsMe] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(() => { api.features().then((r) => { setRows(r); setErr(""); }).catch((e) => setErr(e.message)); }, []);
  useEffect(load, [load]);
  useLive(load, (e) => e.type.startsWith("feature."));

  const groups = useMemo(() => {
    const list = (rows ?? []).filter((f) =>
      (!lane || f.product_id === lane) &&
      (!needsMe || f.pending.some((p) => p.can_decide)) &&
      (showDone || (f.status !== "done" && f.status !== "aborted")));
    const by = new Map<string, FeatureSummary[]>();
    list.forEach((f) => by.set(f.product_id, [...(by.get(f.product_id) ?? []), f]));
    return [...by.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [rows, lane, needsMe, showDone]);

  const lanes = useMemo(() => [...new Set((rows ?? []).map((r) => r.product_id))].sort(), [rows]);
  const [productNames, setProductNames] = useState<Record<string, string>>({});
  useEffect(() => {
    api.products().then((p) => setProductNames(Object.fromEntries(
      Object.entries(p).filter(([, v]) => v.name).map(([k, v]) => [k, v.name as string])))).catch(() => {});
  }, []);
  const names: Record<string, string> = { ...productNames, ...Object.fromEntries(me.lanes.map((l) => [l.id, l.name])) };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Features</h1>
          <p className="muted">Where every feature is on its way to production, and what it's waiting for.</p>
        </div>
        <Link className="btn" to="/intake">New intent</Link>
      </div>
      <div className="board-filters">
        <label className="visually-hidden" htmlFor="lane">Product</label>
        <select id="lane" value={lane} onChange={(e) => setLane(e.target.value)}>
          <option value="">All products</option>
          {lanes.map((l) => <option key={l} value={l}>{names[l] ?? l}</option>)}
        </select>
        <div className="seg" role="group" aria-label="Filter">
          <button aria-pressed={!needsMe} onClick={() => setNeedsMe(false)}>All</button>
          <button aria-pressed={needsMe} onClick={() => setNeedsMe(true)}>Needs my decision</button>
        </div>
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" style={{ width: "auto" }} checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />
          Show released and stopped
        </label>
      </div>
      {err && <p className="error-note" role="alert" style={{ marginTop: 16 }}>{err}</p>}
      {rows && groups.length === 0 && (
        <div className="panel empty" style={{ marginTop: 24 }}>
          <h2>No features here yet</h2>
          <p>Describe what you want to build and agents will start on discovery.</p>
          <Link className="btn" to="/intake">New intent</Link>
        </div>
      )}
      {groups.map(([pid, list]) => (
        <section key={pid} className="lane-group" aria-labelledby={`lane-${pid}`}>
          <h2 id={`lane-${pid}`}>{names[pid] ?? pid} <span className="faint small">{list.length} in flight</span></h2>
          <div className="rows">
            {list.map((f) => (
              <Link key={f.feature_id} to={`/features/${f.feature_id}`} className="row">
                <div>
                  <div className="row-title">{f.title}</div>
                  <div className="row-id">{f.feature_id}{f.created_by ? `, from ${f.created_by}` : ""}</div>
                </div>
                <Rail stations={f.rail} compact />
                <div className="row-status">
                  <Status f={f} />
                  <span className="small faint">
                    {f.risk_tier ? `${RISK[f.risk_tier]}. ` : ""}
                    {f.lanes_touched && f.lanes_touched.length ? `Also changes ${f.lanes_touched.join(", ")}. ` : ""}
                    {ago(f.updated_at)}
                  </span>
                </div>
                <div className="row-cost">{money(f.cost_usd)}</div>
              </Link>
            ))}
          </div>
        </section>
      ))}
    </>
  );
}
