import { useEffect, useState } from "react";
import { api, type DecisionBriefData, type LineComment } from "../api";

/** The top of a gate: the document in a few lines, then what changed since the version sent back. */
export function DecisionBrief({ featureId, interruptId }: { featureId: string; interruptId: string }) {
  const [b, setB] = useState<DecisionBriefData | null>(null);
  useEffect(() => {
    setB(null);
    api.decisionBrief(featureId, interruptId).then(setB).catch(() => setB(null));
  }, [featureId, interruptId]);
  if (!b) return null;
  const changed = b.changed;
  return (
    <>
      {b.summary.lines.length > 0 && (
        <section className="brief-box" aria-labelledby="brief-h">
          <h3 id="brief-h">In two minutes</h3>
          <ul>{b.summary.lines.map((l) => <li key={l}>{l}</li>)}</ul>
          <p className="small faint">{b.summary.by === "agent" ? "Summarised by an agent from the document below." : "Taken from the document below."}</p>
        </section>
      )}
      {changed && (
        <details className="changed" open>
          <summary>
            <strong>Changed since version {changed.from_version}</strong>
            <span className="small muted"> ({changed.lines.filter((l) => l.op === "+" || l.op === "-").length} lines)</span>
          </summary>
          {changed.asked && <p className="small">You asked: “{changed.asked}”</p>}
          {changed.lines.length === 0 ? <p className="small muted">No changes in the text.</p> : (
            <div className="diff" role="table" aria-label="Changed lines">
              {changed.lines.map((l, i) => (
                <div key={i} role="row" className={`diff-line ${l.op === "+" ? "add" : l.op === "-" ? "del" : l.op === "…" ? "gap" : ""}`}>
                  <span aria-hidden="true">{l.op === "…" ? "⋯" : l.op}</span>
                  <span className="visually-hidden">{l.op === "+" ? "Added: " : l.op === "-" ? "Removed: " : ""}</span>
                  {l.text}
                </div>
              ))}
            </div>
          )}
        </details>
      )}
    </>
  );
}

/** The document as lines you can comment on; comments go back with "Revise". */
export function CommentableDoc({ featureId, kind, comments, onAdd }: {
  featureId: string; kind: string; comments: LineComment[]; onAdd: (c: LineComment) => void;
}) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [text, setText] = useState("");
  useEffect(() => {
    api.artifact(featureId, kind).then((a) => setLines(a.content.split("\n"))).catch(() => setLines([]));
  }, [featureId, kind]);
  if (!lines) return <p className="faint small">Loading…</p>;
  const said = new Set(comments.map((c) => c.quote));
  return (
    <ol className="doc-lines">
      {lines.map((l, i) => {
        if (!l.trim()) return null;
        const quote = l.replace(/^#+\s*|^\s*[-*]\s+/, "").trim();
        return (
          <li key={i} className={`${l.startsWith("#") ? "head" : ""}${said.has(quote) ? " noted" : ""}`}>
            <span className="line-text">{quote}</span>
            {open === i ? (
              <form className="line-form" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { onAdd({ quote, text: text.trim() }); setOpen(null); setText(""); } }}>
                <label className="visually-hidden" htmlFor={`c-${i}`}>Comment on this line</label>
                <input id={`c-${i}`} autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder="What should change here?" />
                <button className="btn" disabled={!text.trim()}>Add</button>
                <button type="button" className="btn secondary" onClick={() => setOpen(null)}>Cancel</button>
              </form>
            ) : (
              <button type="button" className="line-add" onClick={() => { setOpen(i); setText(""); }} aria-label={`Comment on: ${quote.slice(0, 60)}`}>Comment</button>
            )}
          </li>
        );
      })}
    </ol>
  );
}
