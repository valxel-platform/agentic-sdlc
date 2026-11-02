import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type PendingItem } from "../api";
import { ARTIFACTS, GATES, PHASES, RISK, gateLabel, money } from "../vocab";
import { ArtifactBody } from "./Artifact";
import { QualityTable } from "./Quality";
import { RepoPlans } from "./External";

const DELIVERY_FAILED: Record<string, string> = {
  build: "The OpenChoreo build failed", dev: "The dev deployment failed", staging: "The staging deployment failed",
  production: "The production deployment failed",
};

type Mode = null | "revise" | "takeover" | "abort" | "resolve" | "retry" | "accept";

const SEV_TONE: Record<string, string> = { critical: "stop", high: "stop", medium: "wait", low: "plain", info: "plain" };

/** What's being decided, the evidence, and the actions this person is allowed to take. */
export function DecisionPanel({ item, featureId, onDone }: { item: PendingItem; featureId: string; onDone: () => void }) {
  const opened = useRef(Date.now());
  const [mode, setMode] = useState<Mode>(null);
  const [feedback, setFeedback] = useState("");
  const [budget, setBudget] = useState("");
  const [days, setDays] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    opened.current = Date.now();
    setMode(null); setFeedback(""); setBudget(""); setDays(""); setContent(""); setErr("");
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
    item.type === "escalation" && item.phase === "delivery" ? (DELIVERY_FAILED[item.wait ?? ""] ?? "Delivery failed")
    : item.type === "escalation" && item.phase === "security" ? "Security findings block this change"
    : item.type === "escalation" && item.phase === "provisioning" ? "Setting up a new repository failed"
    : item.type === "escalation" ? `${PHASES[item.phase ?? ""] ?? item.phase} needs a person`
    : item.type === "handback" ? `You took over ${gateLabel(item.gate)}`
    : gate?.question ?? "Decision";
  const kind = item.artifact?.kind;
  const delivery = item.phase === "delivery";
  const security = item.phase === "security";
  const fixLabel = item.stage === "pr" ? "Have agents fix them" : "Scan again";
  const retryLabel = { build: "Rebuild", dev: "Deploy again", staging: "Deploy again", production: "Ask for G6 again" }[item.wait ?? ""] ?? "Try again";

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
        {security && (
          <p className="muted small" style={{ marginTop: 6 }}>
            {item.stage === "pr" ? "Agents can fix these and push to the same pull requests. " : "Fix forward, then scan again. "}
            Accepting the risk records a waiver with your reason, valid for up to {item.max_waiver_days ?? 90} days.
          </p>
        )}
        {delivery && (
          <p className="muted small" style={{ marginTop: 6 }}>
            {retryLabel} tries the failed step again. Carry on anyway moves the feature forward without it.
          </p>
        )}
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

      {security && item.blocking && (
        <div>
          <h3>Blocking findings</h3>
          <ul className="sec-findings">
            {item.blocking.map((f) => (
              <li key={f.fingerprint}>
                <span className={`chip ${SEV_TONE[f.severity] ?? "plain"}`}>{f.severity}</span>
                <span>{f.url ? <a href={f.url} target="_blank" rel="noreferrer">{f.title}</a> : f.title}</span>
                <span className="small muted">{f.repo.split("/").pop()}, {f.tool}{f.rule ? `, ${f.rule}` : ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!security && item.findings && item.findings.length > 0 && (
        <div>
          <h3>{delivery ? "What went wrong" : "What the evaluators found"}</h3>
          <ul className="findings">{item.findings.map((f) => <li key={f}>{f}</li>)}</ul>
        </div>
      )}

      {item.new_repos && item.new_repos.length > 0 && (
        <div>
          <h3>{item.type === "gate" ? "Approving also creates these repositories" : "New repositories"}</h3>
          <RepoPlans plans={item.new_repos} />
        </div>
      )}

      {item.quality && item.quality.suites.length > 0 && (
        <div>
          <h3>Test and security evidence</h3>
          <QualityTable suites={item.quality.suites} waivers={item.quality.waivers} />
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
          {mode === "accept" && (
            <>
              <label className="field">Why is this risk acceptable? (recorded with the release)
                <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} autoFocus />
              </label>
              <label className="field">Valid for how many days
                <input inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)}
                  placeholder={String(item.max_waiver_days ?? 90)} />
              </label>
            </>
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
            {item.type === "escalation" && security && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => send("retry")}>{fixLabel}</button>
                <button className="btn secondary" disabled={busy} onClick={() => setMode("accept")}>Accept the risk</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {mode === "accept" && (
              <button className="btn" disabled={busy || !feedback.trim()}
                onClick={() => send("accept_risk", days ? { expires_days: Number(days) } : {})}>Accept the risk</button>
            )}
            {item.type === "escalation" && delivery && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => send("resolved")}>Carry on anyway</button>
                <button className="btn secondary" disabled={busy} onClick={() => send("retry")}>{retryLabel}</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {item.type === "escalation" && !delivery && !security && mode === null && (
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
