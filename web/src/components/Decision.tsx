import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type PendingItem } from "../api";
import Markdown from "react-markdown";
import { ACTION_LABEL, ARTIFACTS, GATES, HORIZONS, MODELS, PHASES, RISK, SEVERITY, gateLabel, money, threadPath } from "../vocab";
import { ArtifactBody } from "./Artifact";
import { QualityTable } from "./Quality";
import { AlarmsFiring, AwsResourcesTable } from "./AwsResources";
import { DesignReviewPanel, RepoPlans, SimilarList } from "./External";

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
  const [picked, setPicked] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    opened.current = Date.now();
    setMode(null); setFeedback(""); setBudget(""); setDays(""); setContent(""); setErr(""); setPicked(null);
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

  const gate = item.gate === "release"
    ? (item.model === "launch"
      ? { name: "Release", question: "Launch these features together?", approve: "Launch" }
      : { name: "Release", question: "Ship this release train?", approve: "Approve release" })
    : item.gate ? GATES[item.gate] : undefined;
  const incidentGate = item.gate === "incident" || item.gate === "postmortem";
  const roadmapGate = item.gate === "g0" || item.gate === "release" || incidentGate;
  const stopLabel = item.gate === "g0" ? "Drop initiative" : item.gate === "release" ? "Cancel release" : "Stop feature";
  const chosen = picked ?? (item.actions ?? []).map((a) => a.id);
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
          <Link to={threadPath(featureId)}>{featureId}</Link>{item.title ? ` ${item.title}` : ""}
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

      {item.summary && <p>{item.summary}</p>}

      {item.type !== "handback" && !roadmapGate && (
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

      {item.gate === "g0" && item.initiative && (
        <div>
          <h3>The initiative</h3>
          <dl className="kv plain-kv">
            <dt>Goal</dt><dd>{item.initiative.goal || (item.initiative.new_goal ? `New: ${item.initiative.new_goal.title} (measure: ${item.initiative.new_goal.measure})` : "None")}</dd>
            <dt>Measure</dt><dd>{item.initiative.measure || "None"}</dd>
            <dt>Horizon</dt><dd>{HORIZONS[item.initiative.horizon] ?? item.initiative.horizon}</dd>
            <dt>Budget</dt><dd>{money(item.initiative.budget_usd)} for all its features</dd>
            <dt>Lanes</dt><dd>{item.initiative.lanes.join(", ")}</dd>
            <dt>Releases as</dt><dd>{MODELS[item.initiative.release_model] ?? item.initiative.release_model}</dd>
          </dl>
          <h3>Features it would start</h3>
          <ol className="plain-ol">{item.initiative.candidates.map((c) => <li key={c.title}><strong>{c.title}</strong>{c.description ? `: ${c.description}` : ""}</li>)}</ol>
        </div>
      )}

      {item.gate === "incident" && (
        <div>
          <dl className="kv plain-kv">
            <dt>Severity</dt><dd><span className={`chip ${item.severity === "sev1" || item.severity === "sev2" ? "stop" : "wait"}`}>{SEVERITY[item.severity ?? ""] ?? item.severity}</span></dd>
            <dt>Still firing</dt><dd>{item.firing ?? 0} alert{item.firing === 1 ? "" : "s"} or issue{item.firing === 1 ? "" : "s"}</dd>
            {(item.suspects ?? []).length > 0 && (<><dt>Recent changes</dt><dd>
              {item.suspects!.map((s, n) => <span key={s.feature_id}>{n > 0 && ", "}<Link to={threadPath(s.feature_id)}>{s.feature_id}</Link> {s.title}</span>)}
            </dd></>)}
          </dl>
          <h3>Proposed actions</h3>
          {(item.actions ?? []).length === 0 ? <p className="small muted">None proposed.</p> : (
            <ul className="plain-list action-list">
              {item.actions!.map((a) => (
                <li key={a.id}>
                  <label className="radio">
                    <input type="checkbox" disabled={!item.can_decide} checked={chosen.includes(a.id)}
                      onChange={(e) => setPicked(e.target.checked ? [...chosen, a.id] : chosen.filter((x) => x !== a.id))} />
                    <span><strong>{ACTION_LABEL[a.type] ?? a.type}</strong>{a.text ? `: ${a.text}` : ""}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <h3>Signals</h3>
          <ul className="plain-list small">
            {(item.signals ?? []).slice(-8).map((s, n) => (
              <li key={`${s.fingerprint}-${n}`}>
                <span className={`chip ${s.status === "firing" ? "stop" : "go"}`}>{s.status}</span>{" "}
                {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title} <span className="faint">({s.source})</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {item.gate === "release" && (
        <div>
          <dl className="kv plain-kv">
            <dt>Kind</dt><dd>{MODELS[item.model ?? ""] ?? item.model}</dd>
            {item.target_date && (<><dt>Target date</dt><dd>{item.target_date}</dd></>)}
            <dt>Features</dt><dd>{(item.features ?? []).map((f, n) => <span key={f}>{n > 0 && ", "}<Link to={threadPath(f)}>{f}</Link></span>)}</dd>
            {(item.dropped ?? []).length > 0 && (<><dt>Dropped</dt><dd>{item.dropped!.join(", ")} (stopped before the release)</dd></>)}
          </dl>
          {item.evidence && (<><h3>Evidence</h3><div className="md small"><Markdown>{item.evidence}</Markdown></div></>)}
        </div>
      )}

      {roadmapGate && item.preview && (
        <div>
          <h3 style={{ marginBottom: 8 }}>{item.gate === "g0" ? "Initiative draft" : item.gate === "incident" ? "Triage"
            : item.gate === "postmortem" ? "Postmortem" : "Release notes"}</h3>
          <div className="evidence md"><Markdown>{item.preview}</Markdown></div>
        </div>
      )}

      {item.roadmap_fit && (
        <div>
          <h3>Roadmap</h3>
          {item.roadmap_fit.unplanned ? (
            <p className="small">Unplanned work: this feature isn't part of an approved initiative.</p>
          ) : (
            <p className="small">
              Delivers <Link to={threadPath(item.roadmap_fit.initiative!.id)}>{item.roadmap_fit.initiative!.id}</Link> {item.roadmap_fit.initiative!.title}
              {" "}({HORIZONS[item.roadmap_fit.initiative!.horizon] ?? item.roadmap_fit.initiative!.horizon}).
              {" "}Initiative budget: {money(item.roadmap_fit.spent_usd)} spent of {money(item.roadmap_fit.budget_usd)}
              {item.roadmap_fit.over && <> <span className="chip stop">Over budget</span></>}
            </p>
          )}
        </div>
      )}

      {item.related_features && item.related_features.length > 0 && (
        <div>
          <h3>Already in this product</h3>
          {item.not_duplicate_reason && <p className="small">The creator says it's different: {item.not_duplicate_reason}</p>}
          <SimilarList items={item.related_features} />
        </div>
      )}

      {item.design_review && <DesignReviewPanel review={item.design_review} />}

      {item.alarms_firing && item.alarms_firing.length > 0 && (
        <div role="status">
          <h3>Alarms firing before production</h3>
          <p className="small">On AWS services this feature created or its components use. Approving ships with them firing.</p>
          <AlarmsFiring alarms={item.alarms_firing} />
        </div>
      )}

      {item.aws_resources && item.aws_resources.resources.length > 0 && (
        <div>
          <h3>{item.gate === "g6" ? "Approving also creates these AWS services in production" : "Approving also approves these AWS services"}</h3>
          <AwsResourcesTable aws={item.aws_resources} />
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
              {mode === "revise" ? (item.gate === "incident" ? "What should triage look at?" : "What should the agents change?") : mode === "retry" ? "Guidance for the next attempt (optional)" : item.gate === "incident" ? "Why is no action needed?" : item.gate === "g0" ? "Why drop this initiative?" : item.gate === "release" ? "Why cancel this release?" : "Why stop this feature?"}
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
                <button className="btn go" disabled={busy || (item.gate === "incident" && chosen.length === 0)}
                  onClick={() => send("approve", item.gate === "incident" ? { action_ids: chosen } : {})}>
                  {item.gate === "incident" ? `Approve ${chosen.length} action${chosen.length === 1 ? "" : "s"}` : gate?.approve ?? "Approve"}</button>
                {item.gate !== "release" && <button className="btn secondary" disabled={busy} onClick={() => setMode("revise")}>
                  {item.gate === "incident" ? "Look again" : item.gate === "postmortem" ? "Rewrite" : "Request changes"}</button>}
                {!roadmapGate && <button className="btn secondary" disabled={busy} onClick={() => send("takeover")}>Take over</button>}
                <span className="spacer" />
                {item.gate === "incident" && <button className="btn secondary" disabled={busy} onClick={() => setMode("abort")}>No action needed</button>}
                {!incidentGate && <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>{stopLabel}</button>}
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
            {mode === "abort" && (item.gate === "incident"
              ? <button className="btn" disabled={busy} onClick={() => send("resolve")}>Close without action</button>
              : <button className="btn danger" disabled={busy} onClick={() => send("abort")}>{stopLabel}</button>)}
            {mode !== null && <button className="btn secondary" disabled={busy} onClick={() => setMode(null)}>Cancel</button>}
          </div>
        </>
      )}
    </section>
  );
}
