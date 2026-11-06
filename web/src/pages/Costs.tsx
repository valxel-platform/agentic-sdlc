import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Me, type ProductCost } from "../api";
import { useLive } from "../live";
import { CostTable } from "../components/Costs";
import { money, threadPath } from "../vocab";

const STATUS: Record<string, string> = { released: "Live", in_progress: "In progress", stopped: "Stopped" };
const ENVS: Record<string, string> = { shared: "Shared" };

function thisMonth(): string {
  return new Date().toISOString().slice(0, 7);
}

export function CostsPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const productId = params.get("product") || me.lanes[0]?.id || "";
  const month = params.get("month") || thisMonth();
  const [c, setC] = useState<ProductCost | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    if (!productId) return;
    api.productCost(productId, month).then((r) => { setC(r); setErr(""); }).catch((e) => setErr(e.message));
  }, [productId, month]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;
  const set = (k: string, v: string) => setParams({ product: productId, month, [k]: v });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Costs</h1>
          <p className="muted">What the product costs to run, and what its features cost to build.</p>
        </div>
        <div className="head-actions">
          {me.lanes.length > 1 && (
            <select aria-label="Product" value={productId} onChange={(e) => set("product", e.target.value)}>
              {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          )}
          <input type="month" aria-label="Month" value={month} max={thisMonth()} onChange={(e) => e.target.value && set("month", e.target.value)} />
          <a className="btn secondary" href={`/api/costs/${encodeURIComponent(productId)}/focus.csv?month=${month}`}>Download FOCUS CSV</a>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {c && (
        <>
          <dl className="kv panel cost-totals">
            <dt>Running the product</dt><dd>{money(c.totals.run_usd)}</dd>
            <dt>Building features</dt><dd>{money(c.totals.build_usd)}</dd>
            <dt>{c.month} in all</dt><dd><strong>{money(c.totals.total_usd)}</strong>
              {c.provisional && <span className="small muted"> (recent days may still change)</span>}</dd>
          </dl>
          <div className="grid-2">
            <section className="panel" aria-labelledby="h-run">
              <h2 id="h-run" className="section-title">Running the product</h2>
              <CostTable rows={c.run.by_category} total={c.totals.run_usd} caption="Run cost by category" />
              {Object.keys(c.run.by_env).length > 0 && (
                <p className="small muted" style={{ padding: "8px 16px 16px" }}>
                  By environment: {Object.entries(c.run.by_env).map(([e, v]) => `${ENVS[e] ?? e} ${money(v)}`).join("; ")}
                </p>
              )}
            </section>
            <section className="panel" aria-labelledby="h-build">
              <h2 id="h-build" className="section-title">Building features this month</h2>
              <CostTable rows={c.build.by_category} total={c.totals.build_usd} caption="Build cost by category" />
            </section>
          </div>
          <section className="panel" aria-labelledby="h-features" style={{ marginTop: 20 }}>
            <h2 id="h-features" className="section-title">Features</h2>
            {c.features.length === 0 ? <p className="small muted" style={{ padding: 16 }}>No features yet.</p> : (
              <table className="quality">
                <thead><tr>
                  <th scope="col">Feature</th><th scope="col">Status</th>
                  <th scope="col" className="num">Build, to date</th><th scope="col" className="num">Build, {c.month}</th>
                  <th scope="col" className="num">Its AWS, {c.month}</th><th scope="col" className="num">AWS approved at G4</th>
                </tr></thead>
                <tbody>
                  {c.features.map((f) => (
                    <tr key={f.feature_id}>
                      <td><Link to={threadPath(f.feature_id)}>{f.title}</Link> <span className="small faint">{f.feature_id}</span></td>
                      <td>{STATUS[f.status] ?? f.status}</td>
                      <td className="num">{money(f.build_to_date_usd)}</td>
                      <td className="num">{money(f.build_month_usd)}</td>
                      <td className="num">{money(f.run_month_usd)}</td>
                      <td className="num">{f.approved_monthly_usd != null ? `${money(f.approved_monthly_usd)}/month` : "–"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </>
  );
}
