import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Markdown from "react-markdown";
import { api, ApiError, type InitiativeView, type PendingItem, type ReleaseView } from "../api";
import { useLive } from "../live";
import { DecisionPanel } from "../components/Decision";
import { HORIZONS, MODELS, money, threadPath, waitingLabel } from "../vocab";

const STATUS: Record<string, string> = {
  drafting: "Drafting", awaiting_approval: "Waiting for G0", approved: "Approved", dropped: "Dropped",
  planned: "Planned: waiting for its features", ready: "Ready for approval", shipped: "Shipped", cancelled: "Cancelled",
};

function Pending({ items, id, title, onDone }: { items: PendingItem[]; id: string; title: string; onDone: () => void }) {
  const mine = items.find((p) => p.can_decide);
  const people = items.filter((p) => p.type !== "external");
  if (mine) return <div style={{ marginBottom: 20 }}><DecisionPanel item={{ ...mine, feature_id: id, title }} featureId={id} onDone={onDone} /></div>;
  if (people.length) return <p className="note" style={{ marginBottom: 20 }}>{waitingLabel(people[0])}: {people[0].approvers.join(", ")}.</p>;
  const ext = items.find((p) => p.type === "external");
  return ext ? <p className="note" style={{ marginBottom: 20 }}>{ext.label ?? waitingLabel(ext)}.</p> : null;
}

function Record({ pr }: { pr: { url?: string; error?: string } }) {
  if (pr.url) return <span>Recorded by <a href={pr.url} target="_blank" rel="noreferrer">this pull request</a></span>;
  if (pr.error) return <span className="error-note">Couldn't open the record pull request: {pr.error}</span>;
  return null;
}

export function InitiativePage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const [v, setV] = useState<InitiativeView | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const load = useCallback(() => {
    api.initiative(id).then((x) => { setV(x); setErr(""); })
      .catch((e) => setErr(e instanceof ApiError && e.status === 404 ? "This initiative doesn't exist, or it's still starting." : e.message));
  }, [id]);
  useEffect(load, [load]);
  useLive(load, (e) => e.feature_id === id || e.type === "feature.updated");
  if (err && !v) return <p className="error-note" role="alert">{err}</p>;
  if (!v) return <p className="faint">Loading…</p>;
  const d = v.draft;

  async function start(index: number) {
    setBusy(index);
    setErr("");
    try {
      const r = await api.startCandidate(id, index);
      nav(threadPath(r.feature_id));
    } catch (e) {
      setErr((e as Error).message);
      setBusy(null);
    }
  }

  return (
    <>
      <div className="feature-head">
        <p className="muted small"><Link to={`/roadmap?product=${v.product_id}`}>Roadmap</Link> / {v.feature_id}</p>
        <h1>{v.title}</h1>
        <div className="meta">
          <span>{STATUS[v.status] ?? v.status}</span>
          {v.created_by && <span>From {v.created_by}</span>}
          {d?.horizon && <span>{HORIZONS[d.horizon] ?? d.horizon}</span>}
          {d?.release_model && <span>{MODELS[d.release_model]}</span>}
          <Record pr={v.record_pr} />
        </div>
      </div>
      {v.error && <p className="error-note" role="alert">The workflow stopped with an error: {v.error}</p>}
      <Pending items={v.pending} id={v.feature_id} title={v.title} onDone={load} />
      {err && <p className="error-note" role="alert">{err}</p>}

      <div className="split">
        <section className="panel" aria-labelledby="h-features">
          <h2 id="h-features" className="section-title">Features</h2>
          {v.status === "approved" && (
            <p className="small muted pad">{v.progress.released} of {v.progress.total} started live.
              {" "}Budget: {money(v.spent_usd)} spent of {money(v.budget_usd)}.</p>
          )}
          <ul className="ext-list">
            {v.candidates.map((c, n) => {
              const started = v.features.find((f) => f.title.toLowerCase() === c.title.toLowerCase());
              return (
                <li key={c.title}>
                  <div className="work-line">
                    {started ? <Link to={threadPath(started.feature_id)}>{c.title}</Link> : <strong>{c.title}</strong>}
                    {started ? <span className={`chip ${started.status === "released" && started.flag_on !== false ? "go" : "plain"}`}>
                        {started.status === "released" ? (started.flag_on === false ? "In production, flag off" : "Live")
                          : started.status === "stopped" ? "Stopped" : started.stage || "Started"}</span>
                      : v.status === "approved" && <button className="btn small-btn" disabled={busy !== null} onClick={() => start(n)}>Start feature</button>}
                  </div>
                  {c.description && <div className="small muted">{c.description}</div>}
                </li>
              );
            })}
            {v.features.filter((f) => !v.candidates.some((c) => c.title.toLowerCase() === f.title.toLowerCase())).map((f) => (
              <li key={f.feature_id}><Link to={threadPath(f.feature_id)}>{f.feature_id}</Link> {f.title} <span className="small faint">(added later)</span></li>
            ))}
          </ul>
        </section>
        {!v.pending.some((p) => p.gate === "g0" && p.can_decide) && (
          <section className="panel" aria-labelledby="h-draft">
            <h2 id="h-draft" className="section-title">The initiative</h2>
            <div className="md pad">{v.content ? <Markdown>{v.content}</Markdown> : <p className="faint">The agent is drafting it.</p>}</div>
          </section>
        )}
      </div>
    </>
  );
}

export function ReleasePage() {
  const { id = "" } = useParams();
  const [v, setV] = useState<ReleaseView | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    api.release(id).then((x) => { setV(x); setErr(""); })
      .catch((e) => setErr(e instanceof ApiError && e.status === 404 ? "This release doesn't exist, or it's still starting." : e.message));
  }, [id]);
  useEffect(load, [load]);
  useLive(load, (e) => e.feature_id === id || e.type === "feature.updated");
  if (err && !v) return <p className="error-note" role="alert">{err}</p>;
  if (!v) return <p className="faint">Loading…</p>;

  return (
    <>
      <div className="feature-head">
        <p className="muted small"><Link to={`/roadmap?product=${v.product_id}`}>Roadmap</Link> / {v.feature_id}</p>
        <h1>{v.title}</h1>
        <div className="meta">
          <span>{MODELS[v.model]}</span>
          <span>{STATUS[v.status] ?? v.status}</span>
          {v.target_date && <span>Target {v.target_date}</span>}
          {v.approved_by && <span>Approved by {v.approved_by}</span>}
          <Record pr={v.record_pr} />
        </div>
      </div>
      {v.error && <p className="error-note" role="alert">The workflow stopped with an error: {v.error}</p>}
      <Pending items={v.pending} id={v.feature_id} title={v.title} onDone={load} />

      <div className="split">
        <section className="panel" aria-labelledby="h-members">
          <h2 id="h-members" className="section-title">Features</h2>
          <p className="small muted pad">
            {v.model === "launch"
              ? "Each feature goes to production with its flag off. Approving the release turns the flags on together."
              : "Each feature waits after staging. Approving the release ships them to production together."}
          </p>
          <ul className="ext-list">
            {v.features.map((f) => (
              <li key={f.feature_id}>
                <div className="work-line">
                  <Link to={threadPath(f.feature_id)}>{f.feature_id}</Link> {f.title}
                  {f.flag_on === true && <span className="chip go">Live</span>}
                  {f.flag_on === false && <span className="chip wait">In production, flag off</span>}
                  {f.flag_on == null && <span className="chip plain">{f.status === "stopped" ? "Stopped" : f.stage || f.status}</span>}
                </div>
              </li>
            ))}
            {v.dropped.map((f) => <li key={f} className="faint small">{f}: stopped, dropped from the release</li>)}
          </ul>
        </section>
        <section className="panel" aria-labelledby="h-notes">
          <h2 id="h-notes" className="section-title">Release notes and evidence</h2>
          <div className="md pad">
            {v.notes ? <Markdown>{v.notes}</Markdown> : <p className="faint">Written once every feature is ready.</p>}
            {v.evidence && <><h3>Evidence</h3><Markdown>{v.evidence}</Markdown></>}
          </div>
        </section>
      </div>
    </>
  );
}
