import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type Feature, type FeatureEvent } from "../api";
import { useLive } from "../live";
import { Rail } from "../components/Rail";
import { ArtifactBody } from "../components/Artifact";
import { DecisionPanel } from "../components/Decision";
import { Delivery, Diagrams, WorkItems } from "../components/External";
import { QualityTable } from "../components/Quality";
import { ARTIFACTS, GATES, PHASES, RISK, gateLabel, money } from "../vocab";

const ORDER = ["OPP", "PRD", "UX", "HLD", "PLAN", "BUILD", "REL", "OPS"];

const WAIT_EVENTS: Record<string, Record<string, string>> = {
  security: { retry: "Security findings sent back", accepted_risk: "Security risk accepted", aborted: "Stopped over security findings" },
  ci_wait: { passed: "CI checks passed", failed: "CI checks failed" },
  build_wait: { passed: "OpenChoreo builds succeeded", retry: "Rebuild requested", resolved: "Build failure waved through", aborted: "Stopped after a failed build" },
  deploy_dev: { deployed: "Deployed to dev" },
  dev_wait: { passed: "Ready in dev", retry: "Dev deployment retried", resolved: "Dev failure waved through", aborted: "Stopped after a failed dev deployment" },
  deploy_staging: { deployed: "Deployed to staging" },
  staging_wait: { passed: "Ready in staging", retry: "Staging deployment retried", resolved: "Staging failure waved through", aborted: "Stopped after a failed staging deployment" },
  production_wait: { passed: "Ready in production", retry: "Production deployment sent back to G6", resolved: "Production failure waved through", aborted: "Stopped after a failed production deployment" },
};

function describe(e: FeatureEvent): string {
  const wait = WAIT_EVENTS[e.node]?.[e.kind];
  if (wait) return e.approver && ["retry", "resolved", "aborted"].includes(e.kind) ? `${wait} by ${e.approver}` : wait;
  const name = PHASES[e.node] ?? (GATES[e.node] ? gateLabel(e.node) : e.node.replace(/_/g, " "));
  switch (e.kind) {
    case "started": return "Intent received";
    case "passed": return `${name} finished${typeof e.attempts === "number" && e.attempts > 1 ? ` after ${e.attempts} attempts` : ""}`;
    case "approved": return `${name} approved${e.approver ? ` by ${e.approver}` : ""}`;
    case "revise": return `${name} sent back${e.approver ? ` by ${e.approver}` : ""}`;
    case "escalated": return `${name} escalated to a person`;
    case "resolved": return `${name.replace("escalate ", "")} resolved by ${e.approver ?? "a person"}`;
    case "retry": return `${name.replace("escalate ", "")}: agents retrying`;
    case "aborted": return `Stopped at ${name}`;
    case "merged": return `Pull requests merged`;
    case "capability_registered": return "Capability added to the catalog";
    case "design_and_architecture_approved": return "Design and architecture both approved";
    case "waiting_for_other_gate": return "Waiting for the other design gate";
    case "done": return "Released";
    case "diagram_published": return `Architecture diagram v${e.version ?? ""} published`;
    case "diagram_withheld": return "Architecture diagram kept off the diagram tool";
    case "diagram_failed": return "Architecture diagram couldn't be published";
    default: return `${name}: ${e.kind.replace(/_/g, " ")}`;
  }
}

export function FeaturePage({ canSync }: { canSync: (productId: string) => boolean }) {
  const { id = "" } = useParams();
  const [f, setF] = useState<Feature | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<string>("");

  const load = useCallback(() => {
    api.feature(id).then((v) => {
      setF(v);
      setErr("");
      setTab((t) => t || ORDER.filter((k) => v.artifacts[k]).slice(-1)[0] || "");
    }).catch((e) => setErr(e instanceof ApiError && e.status === 404 ? "This feature doesn't exist, or it's still starting." : e.message));
  }, [id]);
  useEffect(load, [load]);
  useLive(load, (e) => e.feature_id === id);

  if (err) return <p className="error-note" role="alert">{err}</p>;
  if (!f) return <p className="faint">Loading…</p>;

  const kinds = ORDER.filter((k) => f.artifacts[k]);
  const mine = f.pending.find((p) => p.can_decide);
  const people = f.pending.filter((p) => p.type !== "external");
  const spentPct = f.budget_usd ? Math.min(100, ((f.cost_usd ?? 0) / f.budget_usd) * 100) : 0;
  const prs = Object.entries(f.pull_requests);

  return (
    <>
      <div className="feature-head">
        <p className="muted small"><Link to="/board">Features</Link> / {f.feature_id}</p>
        <h1>{f.title}</h1>
        <div className="meta">
          {f.created_by && <span>Intent from {f.created_by}</span>}
          {f.risk_tier && <span>{RISK[f.risk_tier]}</span>}
          {f.lanes_touched && f.lanes_touched.length > 0 && <span>Also changes {f.lanes_touched.join(", ")}</span>}
          <span title={f.constitution_version}>Governed by {f.constitution_version?.split("+").length ?? 0} constitutions</span>
        </div>
      </div>

      <div className="panel rail-panel"><Rail stations={f.rail} /></div>
      {f.error && <p className="error-note" role="alert" style={{ marginBottom: 20 }}>The pipeline stopped with an error: {f.error}</p>}

      {people.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          {mine ? (
            <DecisionPanel item={{ ...mine, feature_id: f.feature_id, title: f.title }} featureId={f.feature_id} onDone={load} />
          ) : (
            <p className="note">Waiting for {people.map((p) => `${p.gate ? gateLabel(p.gate) : PHASES[p.phase ?? ""]} (${p.approvers.join(", ")})`).join("; ")}.</p>
          )}
        </div>
      )}

      <div className="feature-grid">
        <div className="stack">
          <section className="panel" aria-labelledby="h-what">
            <h2 id="h-what" className="section-title">What happened</h2>
            <ol className="timeline">
              {f.events.map((e, i) => (
                <li key={i} className={e.kind}>
                  <span className="tick" aria-hidden="true" />
                  <div>
                    <div>{describe(e)}</div>
                    {e.detail && !["started", "passed", "diagram_published"].includes(e.kind) && <div className="small muted">{e.detail}</div>}
                    <div className="when">{new Date(e.at).toLocaleString()}</div>
                  </div>
                </li>
              ))}
              {f.running && <li className="current"><span className="tick" aria-hidden="true" /><div className="muted">Agents working…</div></li>}
            </ol>
          </section>
          <section className="panel" aria-labelledby="h-cost">
            <h2 id="h-cost" className="section-title">Cost and effort</h2>
            <dl className="kv">
              <dt>Model spend</dt><dd>{money(f.cost_usd)} of {money(f.budget_usd)} budget</dd>
              <dt></dt><dd><div className="budget" aria-hidden="true"><i style={{ width: `${spentPct}%` }} /></div></dd>
              <dt>Model calls</dt><dd>{f.usage.calls}</dd>
              <dt>Cache hit rate</dt><dd>{Math.round(f.usage.cache_hit_rate * 100)}%</dd>
              <dt>Human time</dt><dd>{f.human_active_minutes.toFixed(1)} min across {f.gate_decisions.length} decisions</dd>
            </dl>
          </section>
        </div>

        <div className="stack">
          <section className="panel" aria-labelledby="h-art">
            <h2 id="h-art" className="section-title">Artifacts</h2>
            {kinds.length === 0 ? <p className="muted" style={{ padding: 16 }}>Agents haven't produced anything yet.</p> : (
              <>
                <div className="tabs" role="tablist">
                  {kinds.map((k) => (
                    <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{ARTIFACTS[k] ?? k}</button>
                  ))}
                </div>
                <div className="artifact-body" role="tabpanel">{tab && <ArtifactBody featureId={f.feature_id} kind={tab} />}</div>
              </>
            )}
          </section>
          {Object.keys(f.quality ?? {}).length > 0 && (
            <section className="panel" aria-labelledby="h-quality">
              <h2 id="h-quality" className="section-title">Tests and security</h2>
              <div style={{ padding: "12px 16px 16px" }}>
                <QualityTable
                  suites={["pr", "build", "dev", "staging", "production"].flatMap((st) =>
                    (f.quality[st]?.suites ?? []).map((x) => ({ ...x, stage: st })))}
                  waivers={f.waivers} />
              </div>
            </section>
          )}
          <Delivery f={f} />
          <Diagrams diagrams={f.diagrams ?? []} />
          {f.work_tracking && (
            <WorkItems featureId={f.feature_id} work={f.work_tracking} canSync={canSync(f.product_id)} onSynced={load} />
          )}
          {prs.length > 0 && (
            <section className="panel" aria-labelledby="h-prs">
              <h2 id="h-prs" className="section-title">Pull requests</h2>
              <dl className="kv">
                {prs.map(([key, pr]) => (
                  <div key={key} style={{ display: "contents" }}>
                    <dt><a href={pr.url} target="_blank" rel="noreferrer">{key}</a></dt>
                    <dd>{pr.state === "merged" ? `Merged by ${pr.merged_by}` : "Open"}{pr.review_score != null ? `, review ${pr.review_score.toFixed(2)}` : ""}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
          {f.gate_decisions.length > 0 && (
            <section className="panel" aria-labelledby="h-dec">
              <h2 id="h-dec" className="section-title">Decisions</h2>
              <dl className="kv">
                {f.gate_decisions.map((g, i) => (
                  <div key={i} style={{ display: "contents" }}>
                    <dt>{g.gate.startsWith("escalation") ? `${PHASES[g.phase]} escalation` : gateLabel(g.gate)}{g.lane && g.lane !== f.product_id ? ` (${g.lane})` : ""}</dt>
                    <dd>{g.decision} by {g.approver}{g.feedback ? `: “${g.feedback}”` : ""}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
        </div>
      </div>
    </>
  );
}
