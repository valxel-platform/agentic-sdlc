import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type PendingItem } from "../api";
import { ARTIFACTS, GATES, PHASES, RISK, gateLabel, money } from "../vocab";
import { ArtifactBody } from "./Artifact";

type Mode = null | "revise" | "takeover" | "abort" | "resolve" | "retry";

/** What's being decided, the evidence, and the actions this person is allowed to take. */
export function DecisionPanel({ item, featureId, onDone }: { item: PendingItem; featureId: string; onDone: () => void }) {
  const opened = useRef(Date.now());
  const [mode, setMode] = useState<Mode>(null);
  const [feedback, setFeedback] = useState("");
  const [budget, setBudget] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    opened.current = Date.now();
    setMode(null); setFeedback(""); setBudget(""); setContent(""); setErr("");
  }, [item.interrupt_id]);

  async function send(decision: string, extra: Record<string, unknown> = {}) {
    setBusy(true);
    setErr("");
    // Time spent with the item open counts as the human's active minutes (capped at two hours).
    const minutes = Math.min(120, Math.round(((Date.now() - opened.current) / 60000) * 10) / 10);
    try {
      await api.decide(featureId, item.interrupt_id, { decision, feedback, active_minutes: minutes, ...extra });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const gate = item.gate ? GATES[item.gate] : undefined;
  const heading =
    item.type === "escalation" ? `${PHASES[item.phase ?? ""] ?? item.phase} needs a person`
    : item.type === "handback" ? `You took over ${gateLabel(item.gate)}`
    : gate?.question ?? "Decision";
  const kind = item.artifact?.kind;

  return (
    <section className="panel decision" aria-labelledby="decision-q">
      <div>
        <p className="muted small">
          <Link to={`/features/${featureId}`}>{featureId}</Link>{item.title ? ` ${item.title}` : ""}
        </p>
        <h2 id="decision-q" className="q">{heading}</h2>
        {item.type === "gate" && (
          <p className="muted">
            {item.lanes && item.lanes.length > 1
              ? `${gateLabel(item.gate)}, decided by each lane in turn: ${item.lanes.join(" then ")}. This is ${item.lane}'s decision.`
              : `${gateLabel(item.gate)} gate, decided by ${item.approvers.join(" or ")}.`}
          </p>
        )}
        {item.type === "escalation" && <p className="muted">{item.reason}</p>}
        {item.type === "handback" && <p className="muted">Finish the work yourself, then hand it back so the pipeline continues.</p>}
      </div>

      {item.type !== "handback" && (
        <dl className="facts">
          {item.score != null && <div><dt>Evaluator score</dt><dd>{item.score.toFixed(2)}</dd></div>}
          {item.loops != null && <div><dt>Agent attempts</dt><dd>{item.loops}</dd></div>}
          <div><dt>Spent so far</dt><dd>{money(item.cost_usd)} of {money(item.budget_usd)}</dd></div>
          {item.risk_tier && <div><dt>Risk</dt><dd>{RISK[item.risk_tier] ?? item.risk_tier}</dd></div>}
        </dl>
      )}

      {item.findings && item.findings.length > 0 && (
        <div>
          <h3>What the evaluators found</h3>
          <ul className="findings">{item.findings.map((f) => <li key={f}>{f}</li>)}</ul>
        </div>
      )}

      {item.pull_requests && item.pull_requests.length > 0 && (
        <div>
          <h3>Pull requests in this decision</h3>
          <ul>{item.pull_requests.map((p) => <li key={p}>{p}</li>)}</ul>
        </div>
      )}

      {kind && (
        <div>
          <h3 style={{ marginBottom: 8 }}>{ARTIFACTS[kind] ?? kind}</h3>
          <div className="evidence"><ArtifactBody featureId={featureId} kind={kind} /></div>
        </div>
      )}

      {!item.can_decide ? (
        <p className="note">Waiting on {item.approvers.join(", ") || "the listed approvers"}. You can follow along, but only they can decide.</p>
      ) : (
        <>
          {mode === "revise" || mode === "retry" || mode === "abort" ? (
            <label className="field">
              {mode === "revise" ? "What should the agents change?" : mode === "retry" ? "Guidance for the next attempt (optional)" : "Why stop this feature?"}
              <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} autoFocus />
            </label>
          ) : null}
          {mode === "retry" && (
            <label className="field">New budget in dollars (optional)
              <input inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)} placeholder={String(item.budget_usd ?? "")} />
            </label>
          )}
          {mode === "resolve" && (
            <label className="field">Your finished {ARTIFACTS[kind ?? ""]?.toLowerCase() ?? "artifact"} (optional: leave empty to keep the agents' last draft)
              <textarea value={content} onChange={(e) => setContent(e.target.value)} style={{ minHeight: 160 }} />
            </label>
          )}
          {err && <p className="error-note" role="alert">{err}</p>}
          <div className="actions">
            {item.type === "gate" && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => send("approve")}>{gate?.approve ?? "Approve"}</button>
                <button className="btn secondary" disabled={busy} onClick={() => setMode("revise")}>Request changes</button>
                <button className="btn secondary" disabled={busy} onClick={() => send("takeover")}>Take over</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {item.type === "handback" && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => send("handback")}>Hand back to agents</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {item.type === "escalation" && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => setMode("resolve")}>Mark resolved</button>
                <button className="btn secondary" disabled={busy} onClick={() => setMode("retry")}>Let agents retry</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {mode === "revise" && (
              <button className="btn" disabled={busy || !feedback.trim()} onClick={() => send("revise")}>Send back with changes</button>
            )}
            {mode === "retry" && (
              <button className="btn" disabled={busy} onClick={() => send("retry", budget ? { budget_usd: Number(budget) } : {})}>Retry with agents</button>
            )}
            {mode === "resolve" && (
              <button className="btn go" disabled={busy} onClick={() => send("resolved", content.trim() ? { artifact_content: content } : {})}>Mark resolved</button>
            )}
            {mode === "abort" && (
              <button className="btn danger" disabled={busy} onClick={() => send("abort")}>Stop feature</button>
            )}
            {mode !== null && <button className="btn secondary" disabled={busy} onClick={() => setMode(null)}>Cancel</button>}
          </div>
        </>
      )}
    </section>
  );
}
