import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type FeatureSummary, type Me } from "../api";
import { useLive } from "../live";
import { Stepper, currentStage } from "../components/Stepper";
import { RISK, ago, money, waitingLabel } from "../vocab";

function Status({ f }: { f: FeatureSummary }) {
  if (f.error) return <span className="chip stop">Failed: see details</span>;
  if (f.status === "done") return <span className="chip go">Released</span>;
  if (f.status === "aborted") return <span className="chip stop">Stopped</span>;
  const p = f.pending[0];
  if (p)
    return (
      <span className={`chip ${p.type === "escalation" ? "stop" : p.type === "external" ? "run" : "wait"}`}>
        {waitingLabel(p, f.product_id)}
      </span>
    );
  return <span className="chip run">Agents working</span>;
}

const CLASS_NAME: Record<string, string> = { phi: "Health data (PHI)", payments: "Payments data" };

/** Kanban columns, derived from each feature's current stage (see Stepper).
 * A card only moves right when its checkpoint is approved. */
const COLUMNS: { label: string; stages: number[] }[] = [
  { label: "Plan & Design", stages: [0, 1, 2] },
  { label: "Build", stages: [3] },
  { label: "Check", stages: [4] },
  { label: "Live", stages: [5, 6] },
];

function columnOf(f: FeatureSummary): number {
  if (f.status === "done") return 3;
  const stage = currentStage(f.rail);
  return COLUMNS.findIndex((c) => c.stages.includes(stage));
}

export function Board({ me }: { me: Me }) {
  const [rows, setRows] = useState<FeatureSummary[] | null>(null);
  const [lane, setLane] = useState("");
  const [needsMe, setNeedsMe] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api
      .features()
      .then((r) => {
        setRows(r);
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, []);
  useEffect(load, [load]);
  useLive(load, (e) => e.type.startsWith("feature."));

  const filtered = useMemo(
    () =>
      (rows ?? []).filter(
        (f) =>
          (!lane || f.product_id === lane) &&
          (!needsMe || f.pending.some((p) => p.can_decide)) &&
          (showDone || (f.status !== "done" && f.status !== "aborted")),
      ),
    [rows, lane, needsMe, showDone],
  );

  const lanes = useMemo(() => [...new Set((rows ?? []).map((r) => r.product_id))].sort(), [rows]);
  const [productNames, setProductNames] = useState<Record<string, string>>({});
  const [dataClass, setDataClass] = useState<Record<string, string>>({});
  useEffect(() => {
    api
      .products()
      .then((p) => {
        setProductNames(
          Object.fromEntries(
            Object.entries(p)
              .filter(([, v]) => v.name)
              .map(([k, v]) => [k, v.name as string]),
          ),
        );
        setDataClass(
          Object.fromEntries(Object.entries(p).map(([k, v]) => [k, String(v.data_class ?? "general")])),
        );
      })
      .catch(() => {});
  }, []);
  const names: Record<string, string> = {
    ...productNames,
    ...Object.fromEntries(me.lanes.map((l) => [l.id, l.name])),
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Features</h1>
          <p className="muted">
            Where every feature is on its way to production, and what it&rsquo;s waiting for. A card only
            moves right when its checkpoint is approved.
          </p>
        </div>
        <Link className="btn" to="/intake">
          New request
        </Link>
      </div>
      <div className="chip-row" role="group" aria-label="Filter by product">
        <button type="button" className="chip-btn" aria-pressed={lane === ""} onClick={() => setLane("")}>
          All products
        </button>
        {lanes.map((l) => (
          <button
            key={l}
            type="button"
            className="chip-btn"
            aria-pressed={lane === l}
            onClick={() => setLane(lane === l ? "" : l)}
          >
            {names[l] ?? l}
          </button>
        ))}
      </div>
      <div className="board-filters">
        <div className="seg" role="group" aria-label="Filter">
          <button aria-pressed={!needsMe} onClick={() => setNeedsMe(false)}>
            All
          </button>
          <button aria-pressed={needsMe} onClick={() => setNeedsMe(true)}>
            Needs my decision
          </button>
        </div>
        <label>
          <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />
          Show released and stopped
        </label>
      </div>
      {err && (
        <p className="error-note" role="alert" style={{ marginTop: 16 }}>
          {err}
        </p>
      )}
      {rows && filtered.length === 0 && (
        <div className="panel empty" style={{ marginTop: 24 }}>
          <h2>No features here yet</h2>
          <p>Describe what you want to build and agents will start on discovery.</p>
          <Link className="btn" to="/intake">
            New request
          </Link>
        </div>
      )}
      <div className="kanban">
        {COLUMNS.map((col, ci) => {
          const cards = filtered.filter((f) => columnOf(f) === ci);
          return (
            <section key={col.label} className="kanban-col" aria-label={col.label}>
              <h2>
                {col.label} · {cards.length}
              </h2>
              {cards.map((f) => (
                <Link key={f.feature_id} to={`/features/${f.feature_id}`} className="kanban-card">
                  <div className="row-title">{f.title}</div>
                  <div className="row-id">
                    <span className="mono">{f.feature_id}</span>
                    {f.created_by ? ` from ${f.created_by}` : ""}
                  </div>
                  <div className="small faint" style={{ marginTop: 4 }}>
                    {names[f.product_id] ?? f.product_id}
                    {dataClass[f.product_id] &&
                      dataClass[f.product_id] !== "general" &&
                      ` · ${CLASS_NAME[dataClass[f.product_id]] ?? dataClass[f.product_id]}`}
                  </div>
                  <div style={{ marginTop: 8 }}>
                    <Stepper stations={f.rail} mini />
                  </div>
                  <div
                    style={{
                      marginTop: 10,
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      gap: 8,
                    }}
                  >
                    <Status f={f} />
                    <span className="small faint">
                      {f.risk_tier ? `${RISK[f.risk_tier]}. ` : ""}
                      {money(f.cost_usd)} · {ago(f.updated_at)}
                    </span>
                  </div>
                </Link>
              ))}
            </section>
          );
        })}
      </div>
    </>
  );
}
