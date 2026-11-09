import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Me, type PilotReport } from "../api";
import { useLive } from "../live";
import { money, threadPath } from "../vocab";

const REASON: Record<string, string> = {
  missing_requirement: "Something required was missing", wrong_scope: "Scope was wrong", wrong_approach: "Wrong approach",
  unclear: "Unclear or ambiguous", quality: "Quality wasn't good enough", format: "Format or structure",
  compliance: "Compliance or security", other: "Something else", unspecified: "No reason given",
};

function pct(v: number | null): string {
  return v == null ? "–" : `${(v * 100).toFixed(1)}%`;
}

function of(a: number, b: number): string {
  return b ? `${a} of ${b}` : "none yet";
}

/** The pilot: what people and agents spent per feature against the 15% target, and what the
 * shadow modes would have decided. Features count when they were created as pilot features. */
export function PilotPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const productId = params.get("product") || me.lanes[0]?.id || "";
  const [rep, setRep] = useState<PilotReport | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    if (!productId) return;
    api.pilotReport(productId).then((r) => { setRep(r); setErr(""); }).catch((e) => setErr(e.message));
  }, [productId]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;
  const t = rep?.totals;
  const over = t?.human_share != null && t.human_share > t.target_human_share;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Pilot</h1>
          <p className="muted">People's and agents' time on pilot features, against the target of {Math.round((t?.target_human_share ?? 0.15) * 100)}% people.</p>
        </div>
        <div className="head-actions">
          {me.lanes.length > 1 && (
            <select aria-label="Product" value={productId} onChange={(e) => setParams({ product: e.target.value })}>
              {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          )}
          <a className="btn secondary" href={`/api/reports/pilot?product_id=${encodeURIComponent(productId)}&format=markdown`}>Download as Markdown</a>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {rep && rep.count === 0 && (
        <div className="panel empty">
          <h2>No pilot features yet</h2>
          <p>Tick "Pilot feature" when you submit an intent. Its cost, people's time and the shadow decisions show up here.</p>
        </div>
      )}
      {rep && t && rep.count > 0 && (
        <>
          <section className="panel" aria-labelledby="h-share">
            <h2 id="h-share" className="section-title">Where the time went</h2>
            <dl className="facts pad">
              <div><dt>People's share</dt><dd className={over ? "error-note" : ""}>{pct(t.human_share)}</dd></div>
              <div><dt>People</dt><dd>{Math.round(t.human_minutes)} min</dd></div>
              <div><dt>Agents</dt><dd>{Math.round(t.agent_minutes)} min</dd></div>
              <div><dt>Features</dt><dd>{rep.done} released of {rep.count}</dd></div>
              <div><dt>Gates approved first time</dt><dd>{pct(t.first_time_approval_rate)}</dd></div>
              <div><dt>Escalations</dt><dd>{t.escalations}</dd></div>
              <div><dt>Model cost</dt><dd>{money(t.cost_usd)} <span className="small faint">(+{money(t.shadow_cost_usd)} shadow)</span></dd></div>
            </dl>
            <p className="small muted pad">People's minutes are decisions timed in this app, plus {rep.estimated_minutes.pr_review} min per
              PR review and {rep.estimated_minutes.pr_comment} per comment (estimates). Agents' minutes are the time their model calls took.</p>
          </section>

          <section className="panel" aria-labelledby="h-features">
            <h2 id="h-features" className="section-title">Features</h2>
            <div className="table-scroll">
              <table className="quality">
                <caption className="visually-hidden">Pilot features</caption>
                <thead><tr>
                  <th scope="col">Feature</th><th scope="col" className="num">Cost</th>
                  <th scope="col" className="num">People</th><th scope="col" className="num">Agents</th>
                  <th scope="col" className="num">People's share</th><th scope="col" className="num">First-time approvals</th>
                  <th scope="col" className="num">Agent retries</th><th scope="col" className="num">CI retries</th>
                  <th scope="col" className="num">Escalations</th>
                </tr></thead>
                <tbody>
                  {rep.features.map((f) => (
                    <tr key={f.feature_id}>
                      <th scope="row"><Link to={threadPath(f.feature_id)}>{f.feature_id}</Link> <span className="small muted">{f.title}</span></th>
                      <td className="num">{money(f.cost_usd)}</td>
                      <td className="num">{Math.round(f.human_minutes)} min</td>
                      <td className="num">{Math.round(f.agent_minutes)} min</td>
                      <td className="num">{pct(f.human_share)}</td>
                      <td className="num">{f.first_time_approvals}/{f.gates}</td>
                      <td className="num">{Object.values(f.loops).reduce((a, b) => a + Math.max(0, b - 1), 0)}</td>
                      <td className="num">{f.ci_retries}</td>
                      <td className="num">{f.escalations}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel" aria-labelledby="h-shadow">
            <h2 id="h-shadow" className="section-title">What the trial rules would have decided</h2>
            <ul className="ext-list">
              <li>
                <strong>Automatic approval at G2, G3 and G4</strong>
                <div className="small muted">
                  Eligible at {of(t.shadow_auto_approval.eligible, t.shadow_auto_approval.gates)} gates. People approved {t.shadow_auto_approval.agreed} of
                  those (saving {Math.round(t.shadow_auto_approval.minutes_saved)} min) and sent back {t.shadow_auto_approval.would_have_missed}.
                </div>
              </li>
              <li>
                <strong>A cheaper critic</strong>
                <div className="small muted">Agreed with the critic on {of(t.shadow_critic.agreed, t.shadow_critic.compared)} documents.</div>
              </li>
              <li>
                <strong>A cheaper reviewer</strong>
                <div className="small muted">Agreed with the reviewer on {of(t.shadow_reviewer.agreed, t.shadow_reviewer.compared)} pull requests.</div>
              </li>
              <li>
                <strong>The old 0–1 critic score</strong>
                <div className="small muted">Would have decided the same as the checklist on {of(t.old_score_agreed.agreed, t.old_score_agreed.compared)} documents.</div>
              </li>
            </ul>
          </section>

          <section className="panel" aria-labelledby="h-reasons">
            <h2 id="h-reasons" className="section-title">Why people sent work back</h2>
            {Object.keys(t.revise_reasons).length === 0 ? <p className="pad muted">Nothing was sent back.</p> : (
              <ul className="ext-list">
                {Object.entries(t.revise_reasons).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
                  <li key={k}><span>{REASON[k] ?? k}</span> <span className="small muted">{n}</span></li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </>
  );
}
