import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type DesignReview, type SimilarItem, type Diagram, type Feature, type PendingItem, type RepoPlan, type WorkItem, type WorkTracking } from "../api";
import { WAITS } from "../vocab";

const PROVIDER: Record<string, string> = { lucid: "Lucid", drawio: "draw.io", jira: "Jira", openproject: "OpenProject" };
const KIND: Record<string, string> = { epic: "Epic", story: "Story", task: "Task" };
const STATUS: Record<WorkItem["status"], [string, string]> = {
  todo: ["To do", "plain"], in_progress: ["In progress", "run"], done: ["Done", "go"], cancelled: ["Cancelled", "stop"],
};

export function Diagrams({ diagrams }: { diagrams: Diagram[] }) {
  if (!diagrams.length) return null;
  const list = [...diagrams].reverse();
  return (
    <section className="panel" aria-labelledby="h-diagrams">
      <h2 id="h-diagrams" className="section-title">Architecture diagrams</h2>
      <ul className="ext-list">
        {list.map((d, i) => (
          <li key={d.version}>
            <div>
              <a href={d.url} target="_blank" rel="noreferrer">Version {d.version} in {PROVIDER[d.provider] ?? d.provider}</a>
              {i === 0 && list.length > 1 && <span className="chip plain" style={{ marginLeft: 8 }}>Latest</span>}
            </div>
            <div className="small muted">
              {d.file_url ? <>Committed as <a href={d.file_url} target="_blank" rel="noreferrer">{d.file}</a></> : d.title}
              {d.stub ? ". Offline stand-in: Lucid isn't connected" : ""}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function WorkItems({ featureId, work, canSync, onSynced }: {
  featureId: string; work: WorkTracking; canSync: boolean; onSynced: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const name = PROVIDER[work.provider] ?? work.provider;

  async function sync() {
    setBusy(true);
    setErr("");
    try { await api.syncWork(featureId); onSynced(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <section className="panel" aria-labelledby="h-work">
      <div className="panel-head">
        <h2 id="h-work" className="section-title">{name} items <span className="faint small">project {work.project}</span></h2>
        {canSync && (work.error || work.items.length > 0) && (
          <button className="btn secondary small-btn" disabled={busy} onClick={sync}>{busy ? "Syncing…" : "Sync now"}</button>
        )}
      </div>
      {(work.error || err) && (
        <p className="error-note" role="alert" style={{ margin: "0 16px 12px" }}>
          Couldn't update {name}: {err || work.error}. Agents carry on; the next step syncs again.
        </p>
      )}
      {work.items.length === 0 ? (
        <p className="muted small" style={{ padding: "0 16px 16px" }}>The epic is created in {name} when the intent is approved at G1.</p>
      ) : (
        <ul className="ext-list">
          {work.items.map((w) => {
            const [label, tone] = STATUS[w.status] ?? [w.status, "plain"];
            return (
              <li key={w.item} className={`work ${w.kind}`}>
                <div className="work-line">
                  <span className="faint small work-kind">{KIND[w.kind]}</span>
                  {w.url ? <a href={w.url} target="_blank" rel="noreferrer">{w.key}</a> : <span>{w.key}</span>}
                  <span className={`chip ${tone}`}>{label}</span>
                </div>
                <div className="small muted work-summary">{w.summary.replace(/^\[[^\]]+\]\s*/, "")}</div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

const STAGES: [string, string][] = [
  ["ci", "CI checks"], ["build", "OpenChoreo build"], ["change_set", "Change set's environment"], ["dev", "Dev"], ["staging", "Staging"], ["production", "Production"],
  ["rollout", "Store rollout"],
];
const RESULT: Record<string, [string, string]> = {
  success: ["Passed", "go"], failure: ["Failed", "stop"], pending: ["In progress", "run"],
  security: ["Security review", "wait"], accepted: ["Passed, risk accepted", "go"],
};

function short(name?: string): string {
  return (name ?? "").split("/").pop() ?? "";
}

/** CI, builds and deployments: what passed, what is running now, and where each component runs. */
export function Delivery({ f }: { f: Feature }) {
  const waiting = f.pending.find((p) => p.type === "external") as (PendingItem & { targets?: Record<string, string>[] }) | undefined;
  const stages = STAGES.filter(([k]) => f.delivery?.[k] || waiting?.wait === k);
  const deployed = Object.values(f.deployments ?? {});
  if (!stages.length && !deployed.length) return null;
  return (
    <section className="panel" aria-labelledby="h-delivery">
      <h2 id="h-delivery" className="section-title">Delivery</h2>
      <ul className="ext-list">
        {stages.map(([key, label]) => {
          const now = waiting?.wait === key;
          const res = f.delivery?.[key];
          const [text, tone] = now ? ["In progress", "run"] : (RESULT[res?.status ?? "pending"] ?? [res?.status ?? "", "plain"]);
          const rows = now ? (waiting?.targets ?? []) : (res?.results ?? []);
          return (
            <li key={key}>
              <div className="work-line">
                <span className="stage-name">{label}</span>
                <span className={`chip ${tone}`}>{now ? WAITS[key] : text}</span>
              </div>
              <ul className="stage-rows">
                {rows.map((r, i) => {
                  const row = r as Record<string, unknown>;
                  const name = String(row.repo ?? row.component ?? "");
                  const url = String(row.url ?? "");
                  const state = String(row.state ?? "");
                  return (
                    <li key={i} className="small">
                      {url ? <a href={url} target="_blank" rel="noreferrer">{short(name)}{row.number ? ` #${row.number}` : ""}</a>
                        : <span>{short(name)}</span>}
                      {!now && state && state !== "success" && <span className="muted">: {String(row.message || state)}</span>}
                    </li>
                  );
                })}
              </ul>
            </li>
          );
        })}
      </ul>
      {deployed.length > 0 && (
        <dl className="kv">
          {deployed.map((d) => (
            <div key={d.repo} style={{ display: "contents" }}>
              <dt>{short(d.repo)}</dt>
              <dd>{d.kind === "store" ? `${d.env}, through the stores` : `${d.env}${d.release ? `, release ${d.release}` : ""}`}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

const SECURITY: Record<string, string> = {
  secret_scanning: "secret scanning", push_protection: "push protection", dependabot_alerts: "Dependabot alerts",
  dependabot_security_updates: "Dependabot updates", code_scanning_default_setup: "CodeQL", dependency_graph: "dependency graph",
};

const WHERE: Record<string, string> = { released: "released", stopped: "stopped", live: "live" };

/** What the product already has that looks like a request: at intake and at G1. */
export function SimilarList({ items }: { items: SimilarItem[] }) {
  return (
    <ul className="plain-list similar">
      {items.map((m) => (
        <li key={m.id}>
          {m.kind === "feature" ? <Link to={`/features/${m.id}`}>{m.id}</Link> : <strong>{m.id}</strong>} {m.title}{" "}
          <span className="muted small">({WHERE[m.status] ?? `in progress${m.stage ? `, at ${m.stage}` : ""}`})</span>
          {m.duplicate && <> <span className="chip stop">Likely the same</span></>}
        </li>
      ))}
    </ul>
  );
}

/** G4: the design against what the product already has, read from its architecture record and repos. */
export function DesignReviewPanel({ review }: { review: DesignReview }) {
  const record = review.record?.repo ? `${review.record.repo}@${(review.record.sha ?? "").slice(0, 7)}` : null;
  const errors = Object.entries(review.errors ?? {});
  return (
    <div className="design-review">
      <h3>Against the existing design</h3>
      {review.findings.length > 0 && (
        <ul className="findings">{review.findings.map((f) => <li key={f}>{f}</li>)}</ul>
      )}
      <dl className="kv">
        <dt>Builds on</dt>
        <dd>
          {review.builds_on.length === 0 ? "No existing decisions" : (
            <ul className="plain-list">{review.builds_on.map((a) => <li key={a.id}><strong>{a.id}</strong> {a.title}</li>)}</ul>
          )}
        </dd>
        {review.supersedes.length > 0 && (<>
          <dt>Overrides</dt>
          <dd>{review.supersedes.map((s) => `${s.adr} replaces ${s.supersedes}${s.title ? ` (${s.title})` : ""}`).join("; ")}</dd>
        </>)}
        <dt>API changes</dt>
        <dd>
          {review.contracts.length === 0 ? "None" : (
            <ul className="plain-list">
              {review.contracts.map((c) => (
                <li key={c.repo}>
                  {short(c.repo)}: {c.current ? `${c.current} to ${c.next || "?"}` : c.note || "no contract found"}
                  {c.breaking && <> <span className="chip stop">Breaking</span></>}
                </li>
              ))}
            </ul>
          )}
        </dd>
        {review.related.length > 0 && (<>
          <dt>Not mentioned</dt>
          <dd>
            <ul className="plain-list">{review.related.map((a) => <li key={a.id}><strong>{a.id}</strong> {a.title}</li>)}</ul>
          </dd>
        </>)}
        {review.in_flight.length > 0 && (<>
          <dt>In flight</dt>
          <dd>
            <ul className="plain-list">
              {review.in_flight.map((f) => (
                <li key={f.feature}>
                  {f.feature} ({f.title}) also changes{" "}
                  {(f.contracts ?? []).filter((c) => f.repos.includes(c.repo))
                    .map((c) => `${short(c.repo)} to ${c.next}`).join(", ") || f.repos.map(short).join(", ")}
                </li>
              ))}
            </ul>
          </dd>
        </>)}
      </dl>
      <p className="small muted">
        {record ? `Read from ${record}.` : "This product has no architecture record yet."}
        {errors.length > 0 && ` Couldn't read: ${errors.map(([k, v]) => `${k} (${v})`).join("; ")}.`}
      </p>
    </div>
  );
}

/** What G4 approves: each new repo and the settings it will get. */
export function RepoPlans({ plans }: { plans: RepoPlan[] }) {
  return (
    <ul className="repo-plans">
      {plans.map((p) => (
        <li key={p.repo}>
          <div><strong>{p.repo}</strong> <span className="chip plain">{p.role}</span></div>
          <div className="small muted">{p.purpose}</div>
          <div className="small">
            From {p.template ?? "an empty repo"}, {p.visibility ?? "private"}. Merges need {p.ruleset?.required_approvals ?? 1} approval
            {(p.ruleset?.required_approvals ?? 1) === 1 ? "" : "s"} and checks {(p.ruleset?.required_checks ?? []).join(", ") || "none"}.
            {" "}Turns on {Object.entries(p.security ?? {}).filter(([, on]) => on).map(([k]) => SECURITY[k] ?? k).join(", ")}.
            {" "}Teams: {Object.entries(p.teams ?? {}).map(([t, perm]) => `${t} (${perm})`).join(", ")}.
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Repos created for this feature, and how far their setup got. */
export function NewRepos({ repos }: { repos: RepoPlan[] }) {
  if (!repos.length) return null;
  return (
    <section className="panel" aria-labelledby="h-repos">
      <h2 id="h-repos" className="section-title">New repositories</h2>
      <ul className="ext-list">
        {repos.map((r) => (
          <li key={r.repo}>
            <div className="work-line">
              {r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.repo}</a> : <span>{r.repo}</span>}
              <span className={`chip ${r.status === "provisioned" ? "go" : "stop"}`}>{r.status === "provisioned" ? "Ready" : "Setup failed"}</span>
            </div>
            <div className="small muted">{r.purpose}</div>
            <div className="small">
              {(r.steps ?? []).map((s) => (
                <span key={s.step} className={s.ok ? "step-ok" : "step-bad"} title={s.error ?? s.detail ?? ""}>{s.step}</span>
              ))}
            </div>
            {r.governance_pr && <div className="small">Registered in governance by <a href={r.governance_pr} target="_blank" rel="noreferrer">this pull request</a>.</div>}
          </li>
        ))}
      </ul>
    </section>
  );
}
