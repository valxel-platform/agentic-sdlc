import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Candidate, type RecordPr } from "../api";
import { threadPath } from "../vocab";

/** The pull requests an approval opened: merged here, since the person already approved them. */
export function RecordPrs({ id, canMerge = true }: { id: string; canMerge?: boolean }) {
  const [prs, setPrs] = useState<RecordPr[] | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const load = useCallback(() => { api.records(id).then(setPrs).catch(() => setPrs([])); }, [id]);
  useEffect(load, [load]);
  if (!prs || prs.length === 0) return null;
  async function merge(key: string) {
    setBusy(key);
    setErr("");
    try { await api.mergeRecord(id, key); load(); } catch (e) { setErr((e as Error).message); } finally { setBusy(""); }
  }
  return (
    <div className="records">
      {prs.map((p) => (
        <div key={p.key} className="record-line">
          <span className={`chip ${p.merged ? "go" : "wait"}`}>{p.merged ? "Merged" : "Open"}</span>
          <a href={p.url} target="_blank" rel="noreferrer">{p.label} pull request #{p.number}</a>
          {!p.merged && canMerge && (
            <button className="btn small-btn" disabled={busy !== ""} onClick={() => merge(p.key)}>
              {busy === p.key ? "Merging…" : "Merge it"}</button>
          )}
        </div>
      ))}
      {prs.some((p) => !p.merged) && canMerge && <p className="small faint">You approved it, so there's no second click on GitHub. Branch protection still applies.</p>}
      {err && <p className="error-note small" role="alert">{err}</p>}
    </div>
  );
}

/** After an initiative is approved: which features to start, together or one at a time. */
export function StartFeatures({ id, candidates }: { id: string; candidates: (Candidate & { started?: boolean })[] }) {
  const nav = useNavigate();
  const open = candidates.map((c, i) => ({ c, i })).filter((x) => !x.c.started);
  const [picked, setPicked] = useState<number[]>(() => open.slice(0, 2).map((x) => x.i));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<{ started: string[]; queued: string[] } | null>(null);
  if (open.length === 0) return null;
  async function go(order: "together" | "sequence") {
    setBusy(true);
    setErr("");
    try {
      const r = await api.startFeatures(id, picked, order);
      if (r.started.length === 1 && !r.queued.length) nav(threadPath(r.started[0]));
      else setDone(r);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  if (done) {
    return (
      <section className="panel next-steps" aria-live="polite">
        <h2 className="section-title">Started</h2>
        <p>{done.started.map((f, n) => <span key={f}>{n > 0 && ", "}<Link to={threadPath(f)}>{f}</Link></span>)}
          {done.queued.length > 0 && <> Then, one at a time as each reaches everyone: {done.queued.join(", ")}.</>}</p>
      </section>
    );
  }
  return (
    <section className="panel next-steps" aria-labelledby="next-h">
      <h2 id="next-h" className="section-title">What next?</h2>
      <p className="small muted">{open.length} feature{open.length === 1 ? "" : "s"} not started yet. Pick which to start.</p>
      <div className="pick-list">
        {open.map(({ c, i }) => (
          <label key={c.title} className="check">
            <input type="checkbox" checked={picked.includes(i)}
              onChange={(e) => setPicked(e.target.checked ? [...picked, i].sort((a, b) => a - b) : picked.filter((x) => x !== i))} />
            <span><strong>{c.title}</strong>{c.description ? <span className="small muted"> {c.description}</span> : null}</span>
          </label>
        ))}
      </div>
      <div className="actions">
        <button className="btn go" disabled={busy || !picked.length} onClick={() => go("together")}>
          Start {picked.length === 1 ? "it" : `these ${picked.length}`} now</button>
        {picked.length > 1 && <button className="btn secondary" disabled={busy} onClick={() => go("sequence")}>Start in order, one at a time</button>}
      </div>
      <p className="small faint">Each starts as a brief linked to the initiative's goal; your first decision on each is the idea approval.</p>
      {err && <p className="error-note small" role="alert">{err}</p>}
    </section>
  );
}

/** After a new product is set up: its first feature, once the product is registered. */
export function FirstFeature({ id }: { id: string }) {
  const nav = useNavigate();
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="next-steps">
      <button className="btn go" disabled={busy} onClick={() => {
        setBusy(true);
        api.firstFeature(id).then((r) => nav(threadPath(r.feature_id))).catch((e) => { setErr((e as Error).message); setBusy(false); });
      }}>Start the first feature</button>
      <span className="small muted"> The first initiative's first feature, from the roadmap.</span>
      {err && <p className="error-note small" role="alert">{err}</p>}
    </div>
  );
}

/** After a feature ships: the next one in its initiative. */
export function NextInInitiative({ initiativeId }: { initiativeId: string }) {
  const nav = useNavigate();
  const [next, setNext] = useState<{ title: string; index: number } | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api.initiative(initiativeId).then((v) => {
      const i = v.candidates.findIndex((c) => !c.started);
      setNext(i >= 0 && v.status === "approved" ? { title: v.candidates[i].title, index: i } : null);
    }).catch(() => setNext(null));
  }, [initiativeId]);
  if (!next) return null;
  return (
    <p className="note next-in">
      Next in <Link to={threadPath(initiativeId)}>{initiativeId}</Link>: <strong>{next.title}</strong>{" "}
      <button className="btn small-btn" onClick={() => api.startFeatures(initiativeId, [next.index], "together")
        .then((r) => nav(threadPath(r.started[0]))).catch((e) => setErr((e as Error).message))}>Start it</button>
      {err && <span className="error-note small"> {err}</span>}
    </p>
  );
}
