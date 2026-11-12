import { useState } from "react";
import type { MockupSet } from "../api";

/** G3: each screen of the UX spec as a mockup, in a sandboxed frame (no scripts, nothing loaded
 * from elsewhere), at desktop or phone width; a comment on a screen goes back with "Request changes". */
export function Mockups({ featureId, set, onComment }: { featureId: string; set: MockupSet; onComment?: (quote: string, text: string) => void }) {
  const [at, setAt] = useState(0);
  const [phone, setPhone] = useState<boolean | null>(null);
  const [text, setText] = useState("");
  if (!set.screens.length) {
    return set.findings.length ? <p className="small muted">{set.findings[0]}</p> : null;
  }
  const s = set.screens[Math.min(at, set.screens.length - 1)];
  const narrow = phone ?? s.phone;
  return (
    <section className="mockups" aria-labelledby="mock-h">
      <div className="doc-head">
        <h3 id="mock-h">The screens</h3>
        <div className="segmented small" role="group" aria-label="Width">
          <button type="button" className={!narrow ? "on" : ""} aria-pressed={!narrow} onClick={() => setPhone(false)}>Desktop</button>
          <button type="button" className={narrow ? "on" : ""} aria-pressed={narrow} onClick={() => setPhone(true)}>Phone</button>
        </div>
      </div>
      <div className="mock-tabs" role="tablist">
        {set.screens.map((x, i) => (
          <button key={x.id} type="button" role="tab" aria-selected={i === at} onClick={() => setAt(i)}>
            <span className="mono">{x.id}</span> {x.name}</button>
        ))}
      </div>
      <div className={`mock-frame${narrow ? " phone" : ""}`}>
        <iframe title={`Mockup of ${s.id}: ${s.name}`} sandbox="" src={`/api/features/${encodeURIComponent(featureId)}/mockups/${s.id}`} />
      </div>
      <p className="small faint">A low-fidelity mockup from the UX spec{s.app ? `, for ${s.app}` : ""}: switch its states at the top. Example values only.</p>
      {set.findings.length > 0 && <ul className="findings small">{set.findings.map((f) => <li key={f}>{f}</li>)}</ul>}
      {onComment && (
        <form className="line-form" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onComment(`${s.id} ${s.name}`, text.trim()); setText(""); } }}>
          <label className="visually-hidden" htmlFor="mock-c">Comment on this screen</label>
          <input id="mock-c" value={text} onChange={(e) => setText(e.target.value)} placeholder={`Comment on ${s.id}: what should change on this screen?`} />
          <button className="btn secondary" disabled={!text.trim()}>Add comment</button>
        </form>
      )}
    </section>
  );
}
