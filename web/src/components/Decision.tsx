import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, PROFILE_HELP, PROFILE_LABEL, type AgentNote, type AgentEvals, type PendingItem, type ReleaseCandidate, type ReleaseTraceability } from "../api";
import Markdown from "react-markdown";
import { ACTION_LABEL, ARTIFACTS, GATES, HORIZONS, MODELS, PHASES, RISK, SEVERITY, gateLabel, money, threadPath } from "../vocab";
import { ArtifactBody } from "./Artifact";
import { QualityTable } from "./Quality";
import { AgentSessions } from "./AgentSessions";
import { AlarmsFiring, AwsResourcesTable } from "./AwsResources";
import { DesignReviewPanel, RepoPlans, SimilarList } from "./External";

const DELIVERY_FAILED: Record<string, string> = {
  build: "The OpenChoreo build failed", change_set: "The change set failed in its own environment",
  dev: "The dev deployment failed", staging: "The staging deployment failed",
  production: "The production deployment failed",
};

type Mode = null | "revise" | "takeover" | "abort" | "resolve" | "retry" | "accept";

/** Why work is sent back, so repeated corrections can be found and fixed at their source. */
const REASONS: [string, string][] = [
  ["missing_requirement", "Something required is missing"], ["wrong_scope", "Scope is wrong"],
  ["wrong_approach", "Wrong approach"], ["unclear", "Unclear or ambiguous"], ["quality", "Quality isn't good enough"],
  ["format", "Format or structure"], ["compliance", "Compliance or security"], ["other", "Something else"],
];

/** "Remember this": the correction becomes a proposed lesson for this product's future features. */
function RememberField({ on, setOn, lesson, setLesson }: {
  on: boolean; setOn: (v: boolean) => void; lesson: string; setLesson: (v: string) => void;
}) {
  return (
    <div className="field">
      <label className="radio">
        <input id="decision-remember" type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} />
        <span>Remember this for future features</span>
      </label>
      {on && (
        <label className="field">The lesson, in one line (optional: otherwise it's written from your feedback)
          <input id="decision-lesson" value={lesson} maxLength={400} onChange={(e) => setLesson(e.target.value)}
            placeholder="e.g. Every spec says how long data is kept" />
        </label>
      )}
      <p className="small faint">It's proposed as a pull request to the product's governance repo. Once merged, agents follow it.</p>
    </div>
  );
}

const SEV_TONE: Record<string, string> = { critical: "stop", high: "stop", medium: "wait", low: "plain", info: "plain" };

/** What's being decided, the evidence, and the actions this person is allowed to take. */
const NOTE_LABEL: Record<AgentNote["kind"], string> = {
  open: "Open question", changed: "Changed", assumed: "Assumed", "traded off": "Traded off",
};

/** What the agents assumed, changed, traded off and couldn't settle: open questions first. */
function AgentNotes({ notes }: { notes: AgentNote[] }) {
  return (
    <div>
      <h3>The agents' notes</h3>
      {notes.some((n) => n.kind === "assumed") && (
        <p className="small">Approving confirms what the agents assumed. If an assumption is wrong, ask for changes instead.</p>
      )}
      <ul className="plain-list small">
        {notes.map((n, i) => (
          <li key={i}>
            <strong>{NOTE_LABEL[n.kind]}:</strong> {n.text}
            {(n.task || (n.phase && n.phase !== "")) && (
              <span className="faint"> ({n.task ? `${n.task}, ${n.repo?.split("/").pop() ?? ""}` : n.phase})</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** G6: whether every requirement is traced to passing tests, what the plan defers, and how the
 * work tracker differs from the tool's matrix. */
function ReleaseTrace({ t }: { t: ReleaseTraceability }) {
  return (
    <div role={t.blocks ? "alert" : "status"}>
      <h3>Traceability</h3>
      {t.complete ? <p className="small">Every requirement is traced to passing tests.</p> : (
        <>
          <p className="small">{t.blocks ? "Releasing needs these closed, or an override with your reason:" : "Gaps (a hotfix isn't held for them):"}</p>
          <ul className="plain-list small">{t.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
        </>
      )}
      {t.deferred.length > 0 && (
        <p className="small">Approving accepts what the plan defers: {t.deferred.map((d) => `${d.id} (${d.reason})`).join("; ")}</p>
      )}
      {t.drift.length > 0 && (
        <>
          <p className="small">{t.drift_blocks ? "The work tracker differs from the matrix (this product holds releases for it):"
            : "The work tracker differs from the matrix (a warning):"}</p>
          <ul className="plain-list small">{t.drift.map((d) => <li key={d}>{d}</li>)}</ul>
        </>
      )}
      {t.drift_error && <p className="small muted">The work tracker couldn't be checked: {t.drift_error}</p>}
    </div>
  );
}

/** G6: the product's own agents, by their evals, against the last release that changed them. */
function AgentEvalsPanel({ rows }: { rows: AgentEvals[] }) {
  const stage: Record<string, string> = { pr: "Recorded responses (PR)", dev: "Real model (dev)" };
  const pct = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`);
  return (
    <div role="status">
      <h3>The agents this change ships</h3>
      <table className="quality">
        <caption className="visually-hidden">Agent evals by stage, against the previous release</caption>
        <thead><tr><th scope="col">Agent</th><th scope="col">Evals</th><th scope="col">Passed</th>
          <th scope="col">Last release</th><th scope="col">Change</th></tr></thead>
        <tbody>
          {rows.flatMap((r) => Object.entries(r.stages).map(([st, e]) => (
            <tr key={`${r.repo}-${st}`}>
              <th scope="row">{r.repo.split("/").pop()}</th><td>{stage[st] ?? st}</td>
              <td>{e.passed} of {e.passed + e.failed} ({pct(e.rate)})</td>
              <td>{r.previous[st] ? `${pct(r.previous[st].rate)} (${r.previous_feature})` : "first release"}</td>
              <td>{r.change[st] == null ? "–" : `${r.change[st] >= 0 ? "+" : ""}${Math.round(r.change[st] * 100)} points`}</td>
            </tr>
          )))}
        </tbody>
      </table>
    </div>
  );
}

/** G6 for a shared lane: approving tags each release candidate's commit as the stable version. */
function ReleaseCandidates({ items }: { items: ReleaseCandidate[] }) {
  const said = { published: "published", missing: "not in GitHub Packages yet", unknown: "not checked" };
  return (
    <div role={items.some((p) => p.status === "missing") ? "alert" : "status"}>
      <h3>Packages</h3>
      <p className="small">Approving tags the same commit as the stable version, and the release workflow publishes it.</p>
      <ul className="plain-list small">
        {items.map((p) => (
          <li key={p.repo}>{p.repo}: {p.rc} becomes v{p.version} ({said[p.status]}{p.package ? `, ${p.package.type} ${p.package.name}` : ""})</li>
        ))}
      </ul>
    </div>
  );
}

export function DecisionPanel({ item, featureId, onDone }: { item: PendingItem; featureId: string; onDone: () => void }) {
  const opened = useRef(Date.now());
  const [mode, setMode] = useState<Mode>(null);
  const [feedback, setFeedback] = useState("");
  const [budget, setBudget] = useState("");
  const [days, setDays] = useState("");
  const [content, setContent] = useState("");
  const [picked, setPicked] = useState<string[] | null>(null);
  const [reason, setReason] = useState("");
  const [override, setOverride] = useState("");
  const [remember, setRemember] = useState(false);
  const [lesson, setLesson] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    opened.current = Date.now();
    setMode(null); setFeedback(""); setBudget(""); setDays(""); setContent(item.content ?? ""); setErr(""); setPicked(null);
    setReason(""); setRemember(false); setLesson(""); setOverride("");
  }, [item.interrupt_id, item.content]);

  async function send(decision: string, extra: Record<string, unknown> = {}) {
    setBusy(true);
    setErr("");
    // Time spent with the item open counts as the human's active minutes (capped at two hours).
    const minutes = Math.min(120, Math.round(((Date.now() - opened.current) / 60000) * 10) / 10);
    try {
      await api.decide(featureId, item.interrupt_id, {
        decision, feedback, active_minutes: minutes, ...(reason ? { reason } : {}),
        ...(remember ? { remember: true, ...(lesson.trim() ? { lesson: lesson.trim() } : {}) } : {}), ...extra,
      });
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
  const canRevise = item.allowed ? item.allowed.includes("revise") : true;
  // Whether the person changed the document they were given (only then is it sent).
  const edited = content.trim() !== "" && content.trim() !== (item.content ?? "").trim();
  const learnable = !roadmapGate && !delivery && !security;
  const retryLabel = { build: "Rebuild", change_set: "Try the environment again", dev: "Deploy again", staging: "Deploy again", production: "Ask for G6 again" }[item.wait ?? ""] ?? "Try again";

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
        {item.type === "handback" && (
          <p className="muted">
            {item.content ? "Edit the document below, then hand it back: your version is kept, and the agents learn from what you changed."
              : "Finish the work yourself, then hand it back so the pipeline continues."}
          </p>
        )}
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

      {item.type === "gate" && item.stuck && (
        <div className="note" role="status">
          <strong>Sent back {item.revisions} times.</strong>{" "}
          {canRevise
            ? "Taking it over may be quicker: you edit the document yourself and hand it back, and the agents learn from what you changed."
            : "It can't be sent back again. Take it over to finish the document yourself, or stop the feature."}
        </div>
      )}

      {item.profile && item.profile !== "feature" && item.type === "gate" && (
        <p className="small muted">
          {PROFILE_LABEL[item.profile]} workflow: {PROFILE_HELP[item.profile]}
        </p>
      )}

      {item.spec_preview && (
        <details open>
          <summary>The spec you approve with this design (express: no separate spec gate)</summary>
          <div className="evidence md"><Markdown>{item.spec_preview}</Markdown></div>
        </details>
      )}

      {item.agent_notes && item.agent_notes.length > 0 && <AgentNotes notes={item.agent_notes} />}

      {item.history && item.history.length > 0 && (
        <div>
          <h3>What people asked for before</h3>
          <ol className="plain-ol small">
            {item.history.map((h, n) => (
              <li key={`${h.at}-${n}`}>
                <strong>{h.decision === "revise" ? "Sent back" : h.decision === "retry" ? "Retried" : h.decision === "takeover" ? "Taken over"
                  : h.decision === "handback" ? "Handed back" : "Resolved"}</strong>{" "}
                by {h.approver}{h.reason ? ` (${REASONS.find(([v]) => v === h.reason)?.[1].toLowerCase() ?? h.reason})` : ""}
                {h.feedback ? `: ${h.feedback}` : ""}
              </li>
            ))}
          </ol>
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

      {item.refused && <p role="alert" className="small">{item.refused}</p>}

      {item.traceability && <ReleaseTrace t={item.traceability} />}

      {item.packages && item.packages.length > 0 && <ReleaseCandidates items={item.packages} />}

      {item.agent_evals && item.agent_evals.length > 0 && <AgentEvalsPanel rows={item.agent_evals} />}

      {item.agent_sessions && item.agent_sessions.length > 0 && (
        <details>
          <summary className="small">How the agents built it ({item.agent_sessions.length} sessions)</summary>
          <AgentSessions sessions={item.agent_sessions} caption="Coder and reviewer sessions, with their traces" />
        </details>
      )}

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

      {item.performance && item.performance.length > 0 && (
        <div>
          <h3>Performance tests</h3>
          <ul className="plain-list small">
            {item.performance.map((d) => (
              <li key={d.repo}>
                <strong>{d.repo.split("/").pop()}</strong>{" "}
                {d.level === "none" ? "none needed" : d.level === "smoke" ? "smoke test on the pull request" : "load test in staging"}
                {d.reasons.length > 0 && <span className="muted">: {d.reasons.join("; ")}</span>}
                {d.soak && <span className="muted"> (data grows: watch the nightly soak test)</span>}
              </li>
            ))}
          </ul>
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
          {(mode === "revise" || mode === "retry" || mode === "abort") && !incidentGate && (
            <label className="field">Main reason
              <select id="decision-reason" value={reason} onChange={(e) => setReason(e.target.value)}>
                <option value="">Choose one (optional)</option>
                {REASONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          )}
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
            <>
              <label className="field">{item.content ? `Finish the ${ARTIFACTS[kind ?? ""]?.toLowerCase() ?? "document"} (the agents' last draft; keep it as it is if it's fine)`
                : `Your finished ${ARTIFACTS[kind ?? ""]?.toLowerCase() ?? "artifact"} (optional: leave empty to keep the agents' last draft)`}
                <textarea id="decision-content" value={content} onChange={(e) => setContent(e.target.value)} style={{ minHeight: 240 }} />
              </label>
              <label className="field">What was wrong with it? (optional; helps the agents learn)
                <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} />
              </label>
            </>
          )}
          {item.type === "handback" && mode === null && (
            <>
              {item.content ? (
                <label className="field">{ARTIFACTS[kind ?? ""] ?? "Document"}
                  <textarea id="decision-content" value={content} onChange={(e) => setContent(e.target.value)} style={{ minHeight: 320 }} />
                </label>
              ) : null}
              <label className="field">What did you change, and why? (optional; helps the agents learn)
                <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} />
              </label>
            </>
          )}
          {learnable && ((item.type === "handback" && mode === null) || mode === "revise" || mode === "retry" || mode === "resolve") && (
            <RememberField on={remember} setOn={setRemember} lesson={lesson} setLesson={setLesson} />
          )}
          {err && <p className="error-note" role="alert">{err}</p>}
          <div className="actions">
            {item.type === "gate" && mode === null && (
              <>
                {item.traceability?.blocks && (
                  <label className="field" style={{ width: "100%" }}>
                    Release anyway: why (recorded with the release; only {item.traceability.override_approvers.join(", ") || "the traceability approvers"} can)
                    <textarea id="override-reason" value={override} onChange={(e) => setOverride(e.target.value)} />
                  </label>
                )}
                <button className="btn go"
                  disabled={busy || (item.gate === "incident" && chosen.length === 0) || (!!item.traceability?.blocks && !override.trim())}
                  onClick={() => send("approve", item.gate === "incident" ? { action_ids: chosen }
                    : item.traceability?.blocks ? { override_reason: override.trim() } : {})}>
                  {item.gate === "incident" ? `Approve ${chosen.length} action${chosen.length === 1 ? "" : "s"}` : gate?.approve ?? "Approve"}</button>
                {item.gate !== "release" && canRevise && <button className="btn secondary" disabled={busy} onClick={() => setMode("revise")}>
                  {item.gate === "incident" ? "Look again" : item.gate === "postmortem" ? "Rewrite" : "Request changes"}</button>}
                {!roadmapGate && <button className={item.stuck ? "btn" : "btn secondary"} disabled={busy} onClick={() => send("takeover")}>Take over</button>}
                <span className="spacer" />
                {item.gate === "incident" && <button className="btn secondary" disabled={busy} onClick={() => setMode("abort")}>No action needed</button>}
                {!incidentGate && <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>{stopLabel}</button>}
              </>
            )}
            {item.type === "handback" && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => send("handback", edited ? { artifact_content: content } : {})}>
                  {edited ? "Hand back with my version" : "Hand back to agents"}</button>
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
                <button className="btn go" disabled={busy} onClick={() => setMode("resolve")}>{item.content ? "Finish it myself" : "Mark resolved"}</button>
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
              <button className="btn go" disabled={busy} onClick={() => send("resolved", (item.content ? edited : content.trim()) ? { artifact_content: content } : {})}>
                {item.content && edited ? "Resolve with my version" : "Mark resolved"}</button>
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
