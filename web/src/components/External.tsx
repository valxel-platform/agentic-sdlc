import { useState } from "react";
import { api, type Diagram, type Feature, type PendingItem, type WorkItem, type WorkTracking } from "../api";
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
  ["ci", "CI checks"], ["build", "OpenChoreo build"], ["dev", "Dev"], ["staging", "Staging"], ["production", "Production"],
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
              <dd>{d.env}{d.release ? `, release ${d.release}` : ""}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
