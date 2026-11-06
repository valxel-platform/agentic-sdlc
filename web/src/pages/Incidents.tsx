import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Markdown from "react-markdown";
import { api, ApiError, type IncidentRow, type IncidentView, type Me } from "../api";
import { useLive } from "../live";
import { DecisionPanel } from "../components/Decision";
import { ACTION_LABEL, SEVERITY, threadPath, waitingLabel } from "../vocab";

const STATUS: Record<string, string> = {
  triaging: "Triage in progress", awaiting_decision: "Waiting for on-call", mitigating: "Acting",
  monitoring: "Watching for recovery", postmortem: "Postmortem", closed: "Closed", merged: "Merged",
};

function Sev({ s }: { s: string }) {
  return <span className={`chip ${s === "sev1" || s === "sev2" ? "stop" : s === "sev3" ? "wait" : "plain"}`}>{SEVERITY[s] ?? s}</span>;
}

function ReportProblem({ me, onCancel }: { me: Me; onCancel: () => void }) {
  const nav = useNavigate();
  const [product, setProduct] = useState(me.lanes[0]?.id ?? "");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState("sev3");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const r = await api.reportProblem({ product_id: product, title, description, severity });
      nav(threadPath(r.feature_id));
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  }
  return (
    <form className="panel form-panel" onSubmit={submit}>
      <h2 className="section-title">Report a problem</h2>
      <p className="small muted">For problems users hit in production. An agent investigates; on-call decides what to do. Don't include patient details.</p>
      {me.lanes.length > 1 && (
        <label className="field">Product
          <select value={product} onChange={(e) => setProduct(e.target.value)}>
            {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
          </select>
        </label>
      )}
      <label className="field">What's wrong<input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={3} /></label>
      <label className="field">What happened, and what did you expect?<textarea value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      <label className="field">How bad is it
        <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="sev1">SEV1: patients or clinics can't work</option>
          <option value="sev2">SEV2: a key workflow is broken</option>
          <option value="sev3">SEV3: something is wrong, with a workaround</option>
          <option value="sev4">SEV4: minor</option>
        </select>
      </label>
      {err && <p className="error-note" role="alert">{err}</p>}
      <div className="actions">
        <button className="btn" disabled={busy}>Report problem</button>
        <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export function IncidentsPage({ me }: { me: Me }) {
  const [rows, setRows] = useState<IncidentRow[] | null>(null);
  const [err, setErr] = useState("");
  const [reporting, setReporting] = useState(false);
  const load = useCallback(() => { api.incidents().then((r) => { setRows(r); setErr(""); }).catch((e) => setErr(e.message)); }, []);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated" || e.type === "feature.created");
  const open = (rows ?? []).filter((r) => r.status !== "closed");
  const closed = (rows ?? []).filter((r) => r.status === "closed");
  const Row = ({ r }: { r: IncidentRow }) => (
    <li>
      <div className="work-line"><Sev s={r.severity} /><Link to={threadPath(r.feature_id)}>{r.title}</Link>
        <span className="chip plain">{STATUS[r.status] ?? r.status}</span></div>
      <div className="small muted">{r.feature_id}{r.component ? `, ${r.component.split("/").pop()}` : ""}
        {r.firing > 0 ? `, ${r.firing} still firing` : ""}
        {Object.keys(r.links ?? {}).length > 0 ? `, started ${Object.keys(r.links).join(", ")}` : ""}</div>
    </li>
  );
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Incidents</h1>
          <p className="muted">Production problems from alerts, error tracking and reports. Agents investigate; on-call decides.</p>
        </div>
        <button className="btn" onClick={() => setReporting(true)}>Report a problem</button>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {reporting && <ReportProblem me={me} onCancel={() => setReporting(false)} />}
      <section className="panel" aria-labelledby="h-open">
        <h2 id="h-open" className="section-title">Open</h2>
        {rows && open.length === 0 ? <p className="small muted pad">No open incidents.</p> :
          <ul className="ext-list">{open.map((r) => <Row key={r.feature_id} r={r} />)}</ul>}
      </section>
      {closed.length > 0 && (
        <section className="panel" aria-labelledby="h-closed">
          <h2 id="h-closed" className="section-title">Closed</h2>
          <ul className="ext-list">{closed.map((r) => <Row key={r.feature_id} r={r} />)}</ul>
        </section>
      )}
    </>
  );
}

/** On-call: this incident is the same failure as another one. */
function MergeForm({ id, onDone }: { id: string; onDone: () => void }) {
  const [into, setInto] = useState("");
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr("");
    try {
      await api.mergeIncident(id, into.trim(), "Same failure");
      onDone();
    } catch (x) {
      setErr((x as Error).message);
    }
  }
  return (
    <form className="pad small" onSubmit={(e) => void submit(e)}>
      <label className="field">Same failure as incident
        <input value={into} onChange={(e) => setInto(e.target.value)} placeholder="e.g. EMR-INC-4" pattern="[A-Z0-9]+-INC-[0-9]+" required />
      </label>
      <button className="btn secondary small-btn" type="submit">Merge into it</button>
      {err && <p className="error-note" role="alert">{err}</p>}
    </form>
  );
}

export function IncidentPage() {
  const { id = "" } = useParams();
  const [v, setV] = useState<IncidentView | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    api.incident(id).then((x) => { setV(x); setErr(""); })
      .catch((e) => setErr(e instanceof ApiError && e.status === 404 ? "This incident doesn't exist." : e.message));
  }, [id]);
  useEffect(load, [load]);
  useLive(load, (e) => e.feature_id === id || e.type === "feature.updated");
  const navigate = useNavigate();
  async function split(fingerprint: string) {
    try {
      const r = await api.splitSignal(id, fingerprint);
      if (r.new_incident) navigate(threadPath(r.new_incident));
      else load();
    } catch (x) {
      setErr((x as Error).message);
    }
  }
  if (err && !v) return <p className="error-note" role="alert">{err}</p>;
  if (!v) return <p className="faint">Loading…</p>;
  const mine = v.pending.find((p) => p.can_decide);
  const people = v.pending.filter((p) => p.type !== "external");
  const ext = v.pending.find((p) => p.type === "external");

  return (
    <>
      <div className="feature-head">
        <p className="muted small"><Link to="/incidents">Incidents</Link> / {v.feature_id}</p>
        <h1>{v.title}</h1>
        <div className="meta">
          <Sev s={v.severity} />
          <span>{STATUS[v.status] ?? v.status}</span>
          {v.root?.component ? <span title={v.root.why || undefined}>Starts in {v.root.component}</span>
            : v.component && <span>{v.component}</span>}
          {v.firing > 0 && <span>{v.firing} still firing</span>}
          {v.record_pr?.url && <span>Postmortem in <a href={v.record_pr.url} target="_blank" rel="noreferrer">this pull request</a></span>}
        </div>
      </div>
      {v.error && <p className="error-note" role="alert">The workflow stopped with an error: {v.error}</p>}
      {err && <p className="error-note" role="alert">{err}</p>}
      {mine ? <div style={{ marginBottom: 20 }}><DecisionPanel item={{ ...mine, feature_id: v.feature_id, title: v.title }} featureId={v.feature_id} onDone={load} /></div>
        : people.length > 0 ? <p className="note" style={{ marginBottom: 20 }}>{waitingLabel(people[0])}: {people[0].approvers.join(", ")}.</p>
        : ext ? <p className="note" style={{ marginBottom: 20 }}>{ext.label}.</p> : null}

      <div className="split">
        <section className="panel" aria-labelledby="h-done">
          <h2 id="h-done" className="section-title">What was done</h2>
          {v.taken.length === 0 ? <p className="small muted pad">Nothing yet.</p> : (
            <ul className="ext-list">
              {v.taken.map((t, n) => (
                <li key={`${t.id}-${n}`}>
                  <div className="work-line"><strong>{ACTION_LABEL[t.type] ?? t.type}</strong>
                    <span className={`chip ${t.ok ? "go" : "stop"}`}>{t.ok ? "Done" : "Failed"}</span></div>
                  <div className="small muted">{t.result}{t.by ? `, approved by ${t.by}` : ""}</div>
                </li>
              ))}
            </ul>
          )}
          {Object.keys(v.links).length > 0 && (
            <>
              <h3 className="pad">Started from this incident</h3>
              <ul className="ext-list">
                {Object.entries(v.links).map(([tid, kind]) => <li key={tid}><Link to={threadPath(tid)}>{tid}</Link> <span className="small muted">{kind}</span></li>)}
              </ul>
            </>
          )}
          <h3 className="pad">Signals</h3>
          <ul className="ext-list">
            {v.signals.slice().reverse().map((s, n) => (
              <li key={`${s.fingerprint}-${n}`}>
                <div className="work-line"><span className={`chip ${s.status === "firing" ? "stop" : "go"}`}>{s.status}</span>
                  {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : <span>{s.title}</span>}</div>
                <div className="small muted">{s.source}{s.component ? ` on ${s.component}` : ""}, {new Date(s.at).toLocaleString()}</div>
                {s.labels?.joined_by && (
                  <div className="small">
                    Joined: {s.labels.joined_by}{" "}
                    <button className="btn secondary small-btn" onClick={() => void split(s.fingerprint)}>Not the same failure</button>
                  </div>
                )}
              </li>
            ))}
          </ul>
          {!["merged", "closed"].includes(v.status) && <MergeForm id={v.feature_id} onDone={load} />}
        </section>
        {!mine && (
          <section className="panel" aria-labelledby="h-report">
            <h2 id="h-report" className="section-title">{v.postmortem ? "Postmortem" : "Triage"}</h2>
            <div className="md pad"><Markdown>{v.postmortem || v.triage || "Triage in progress."}</Markdown></div>
          </section>
        )}
      </div>
    </>
  );
}
