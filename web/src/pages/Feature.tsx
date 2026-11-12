import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, PROFILE_HELP, PROFILE_LABEL, type Feature, type FeatureCost, type FeatureEvent, type FeatureOutcome } from "../api";
import { CostTable } from "../components/Costs";
import { AgentSessions } from "../components/AgentSessions";
import { useLive } from "../live";
import { Resize } from "../components/Resize";
import { NotifyPanel, PathPanel } from "../components/Path";
import { NextInInitiative } from "../components/NextSteps";
import { Rail } from "../components/Rail";
import { ArtifactBody } from "../components/Artifact";
import { DecisionPanel } from "../components/Decision";
import { Delivery, Diagrams, NewRepos, WorkItems } from "../components/External";
import { QualityTable } from "../components/Quality";
import { ReadoutTable } from "../components/Outcome";
import { TracePanel } from "../components/Trace";
import { AttachmentList } from "../components/Attachments";
import { ARTIFACTS, GATES, PHASES, RISK, gateLabel, money, threadPath } from "../vocab";

const ORDER = ["OPP", "PRD", "UX", "HLD", "PLAN", "BUILD", "REL", "OPS"];

const WAIT_EVENTS: Record<string, Record<string, string>> = {
  provision: { repo_provisioned: "New repository ready", retry: "Repository setup retried", resolved: "Repository setup finished by hand", aborted: "Stopped during repository setup" },
  security: { retry: "Security findings sent back", accepted_risk: "Security risk accepted", aborted: "Stopped over security findings" },
  ci_wait: { passed: "CI checks passed", failed: "CI checks failed" },
  build_wait: { passed: "OpenChoreo builds succeeded", retry: "Rebuild requested", resolved: "Build failure waved through", aborted: "Stopped after a failed build" },
  cs_env: { building: "Change set's environment created", skipped: "Merged without a change-set environment" },
  cs_build_wait: { passed: "Pull request heads built" },
  cs_deploy: { deployed: "Deployed to the change set's environment" },
  cs_wait: { passed: "The change set works together" },
  change_set_wait: { auto_fix: "Sent back: failed in the change set's environment", retry: "Change-set environment retried",
    resolved: "Change-set failure waved through", aborted: "Stopped after the change set failed" },
  merge_train: { change_set_env_deleted: "Change set's environment deleted" },
  deploy_dev: { deployed: "Deployed to dev" },
  dev_wait: { passed: "Ready in dev", retry: "Dev deployment retried", resolved: "Dev failure waved through", aborted: "Stopped after a failed dev deployment" },
  deploy_staging: { deployed: "Deployed to staging" },
  staging_wait: { instrumentation: "Goal events counted in staging", passed: "Ready in staging", retry: "Staging deployment retried", resolved: "Staging failure waved through", aborted: "Stopped after a failed staging deployment" },
  production_wait: { passed: "Ready in production", retry: "Production deployment sent back to G6", resolved: "Production failure waved through", aborted: "Stopped after a failed production deployment" },
};

// Everything the feature has cost to build: agents, CI, builds, environments, AWS, tests, people.
function BuildCost({ featureId, version }: { featureId: string; version: number }) {
  const [c, setC] = useState<FeatureCost | null>(null);
  useEffect(() => { api.featureCost(featureId).then(setC).catch(() => setC(null)); }, [featureId, version]);
  if (!c) return null;
  return (
    <>
      <h3 className="section-title small">Cost to build</h3>
      <CostTable rows={c.build.by_category} total={c.build.total_usd} caption="Build cost by category" />
      {c.run_30d.total_usd > 0 && (
        <p className="small muted" style={{ padding: "8px 16px 16px" }}>
          Its own AWS services since release, last 30 days: {money(c.run_30d.total_usd)}
        </p>
      )}
    </>
  );
}

// Whether the feature moved its product goal: its events in staging, then readouts after release.
function Outcome({ featureId, version }: { featureId: string; version: number }) {
  const [o, setO] = useState<FeatureOutcome | null>(null);
  useEffect(() => { api.featureOutcome(featureId).then(setO).catch(() => setO(null)); }, [featureId, version]);
  if (!o || o.goals.length === 0) return null;
  const check = o.instrumentation;
  return (
    <section className="panel" aria-labelledby="h-outcome">
      <h2 id="h-outcome" className="section-title">Outcome</h2>
      <div className="pad small">
        <p>Moves {o.goals.join(", ")}.
          {check && check.checked && (check.missing.length > 0
            ? ` In staging, ${check.missing.join(", ")} never fired.`
            : ` All ${check.events.length} of its events fired in staging.`)}
        </p>
        {!o.released_at && <p className="muted">Readouts start once the feature reaches everyone.</p>}
      </div>
      <ReadoutTable readouts={o.readouts} metrics={o.metrics} />
      {o.readouts.length > 0 && <p className="small faint pad">{o.readouts[0].caveat}</p>}
      {o.rollout.length > 0 && (
        <div className="pad small">
          <p className="muted">During the gradual release, users with it on against users with it off:</p>
          <ul className="plain-list">
            {o.rollout.map((r) => <li key={r.percent}><strong>At {r.percent}%</strong>: {r.goal}</li>)}
          </ul>
        </div>
      )}
      {o.upcoming.length > 0 && (
        <p className="small muted pad">
          Next: {o.upcoming.slice(0, 3).map((u) => `${u.window_days}-day readout ${u.due}`).join(", ")}
        </p>
      )}
    </section>
  );
}

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
    case "flag": return "Feature flag";
    case "flag_failed": return "Feature flag couldn't be changed";
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
          {f.profile && f.profile !== "feature" && <span title={PROFILE_HELP[f.profile]}>{PROFILE_LABEL[f.profile]} workflow</span>}
          {f.risk_tier && <span>{RISK[f.risk_tier]}</span>}
          {f.lanes_touched && f.lanes_touched.length > 0 && <span>Also changes {f.lanes_touched.join(", ")}</span>}
          {f.initiative_id ? <span>Delivers <Link to={threadPath(f.initiative_id)}>{f.initiative_id}</Link></span> : <span>Unplanned work</span>}
          {f.release?.release_id && <span>In <Link to={threadPath(f.release.release_id)}>{f.release.release_id}</Link></span>}
          {f.release?.production && f.release.flag && (f.release.flag_on
            ? (f.release.flag_percent !== undefined && f.release.flag_percent < 100
              ? <span className="chip run" title={f.release.flag}>Gradual release: on for {f.release.flag_percent}% of users</span>
              : <span className="chip go" title={f.release.flag}>Live</span>)
            : <span className="chip wait" title={f.release.flag}>In production, flag off</span>)}
          {f.flag && <span>Feature flag <code>{f.flag}</code></span>}
          <span title={f.constitution_version}>Governed by {f.constitution_version?.split("+").length ?? 0} constitutions</span>
        </div>
      </div>

      <div className="panel rail-panel"><Rail stations={f.rail} /></div>
      {!f.initiative_id && <Resize id={f.feature_id} kind="feature" pending={f.pending} />}
      {f.initiative_id && (f.status === "released" || f.status === "done") && <NextInInitiative initiativeId={f.initiative_id} />}
      {f.path && (
        <div className="path-row">
          <PathPanel path={f.path} />
          <NotifyPanel threadId={f.feature_id} watching={!!f.watching} />
        </div>
      )}
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
              {f.config_variant === "canary" && <>
                <dt>Agents' config</dt>
                <dd>Candidate (canary): compared with the config in use in the pilot report</dd>
              </>}
              <dt>Human time</dt><dd>{f.human_active_minutes.toFixed(1)} min across {f.gate_decisions.length} decisions</dd>
              {f.run_cost && <>
                <dt>AWS approved</dt>
                <dd>{money(f.run_cost.approved_monthly_usd.production ?? 0)}/month in production</dd>
                <dt>AWS running at</dt>
                <dd>{f.run_cost.projected_monthly_usd
                  ? <>{money(f.run_cost.projected_monthly_usd.production ?? 0)}/month
                      <span className="small muted"> (reviewed {new Date(f.run_cost.reviewed_at ?? "").toLocaleDateString()})</span></>
                  : <span className="muted">Reviewed 7 and 30 days after release</span>}</dd>
              </>}
            </dl>
            <BuildCost featureId={f.feature_id} version={f.events.length} />
            {f.agent_sessions && f.agent_sessions.length > 0 && (
              <details style={{ padding: "0 16px 16px" }}>
                <summary className="small">Agent sessions ({f.agent_sessions.length})</summary>
                <AgentSessions sessions={f.agent_sessions} caption="What each agent session worked on, and its trace" />
              </details>
            )}
            {f.run_cost && f.run_cost.findings.length > 0 && (
              <div className="small" role="status" style={{ padding: "0 16px 16px" }}>
                {f.run_cost.findings.map((t) => <p key={t}>{t}</p>)}
                {f.run_cost.incidents.map((i) => <Link key={i} to={threadPath(i)}>{i}</Link>)}
              </div>
            )}
          </section>
        </div>

        <div className="stack">
          <Outcome featureId={f.feature_id} version={f.events.length} />
          {(f.attachments ?? []).length > 0 && (
            <section className="panel" aria-labelledby="h-attached">
              <h2 id="h-attached" className="section-title">Attached at intake</h2>
              <div className="pad" style={{ paddingTop: 10 }}>
                <AttachmentList items={f.attachments ?? []}
                  href={(a) => `/api/features/${encodeURIComponent(f.feature_id)}/attachments/${a.id}`} />
              </div>
            </section>
          )}
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
                  suites={["pr", "build", "change_set", "dev", "staging", "production"].flatMap((st) =>
                    (f.quality[st]?.suites ?? []).map((x) => ({ ...x, stage: st })))}
                  waivers={f.waivers} />
              </div>
            </section>
          )}
          <TracePanel featureId={f.feature_id} version={f.events.length} />
          <NewRepos repos={f.new_repos ?? []} />
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
