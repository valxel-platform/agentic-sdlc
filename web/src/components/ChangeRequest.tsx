import { useState } from "react";
import { api } from "../api";

/** Change what was asked for, before the build starts. A gate waiting now goes back to its agents
 * with the change; after the build has started, a follow-up feature is the way. */
export function ChangeRequest({ id, description, built, onDone }: { id: string; description: string; built: boolean; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(description);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [said, setSaid] = useState("");
  if (built) {
    return <p className="small faint change-req">The build has started, so a change to the request is a <a href="/intake">new request</a> now.</p>;
  }
  if (!open) {
    return (
      <p className="small change-req">
        <button type="button" className="linkish" onClick={() => { setText(description); setOpen(true); }}>Change the request</button>
        {said && <span className="muted"> {said}</span>}
      </p>
    );
  }
  async function save() {
    setBusy(true);
    setErr("");
    try {
      const r = await api.changeRequest(id, text);
      setSaid(r.sent_back ? `Changed. ${r.sent_back.toUpperCase()} went back to the agents with your change.` : "Changed. The next step uses it.");
      setOpen(false);
      onDone();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <section className="panel change-req" aria-labelledby="cr-h">
      <h2 id="cr-h" className="section-title">Change the request</h2>
      <label className="field">What you're asking for
        <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <p className="small note">If a decision is waiting, it goes back to its agents with your change highlighted. Earlier approvals stay.</p>
      {err && <p className="error-note small" role="alert">{err}</p>}
      <div className="actions">
        <button className="btn" disabled={busy || text.trim() === description.trim() || text.trim().length < 10} onClick={save}>Save and update the work</button>
        <button className="btn secondary" onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </section>
  );
}
