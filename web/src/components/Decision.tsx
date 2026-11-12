import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, PROFILE_HELP, PROFILE_LABEL, type AgentNote, type AgentEvals, type LineComment, type PendingItem, type ReleaseCandidate, type ReleaseTraceability } from "../api";
import { CommentableDoc, DecisionBrief } from "./DecisionBrief";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
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

type Mode = null | "revise" | "takeover" | "abort" | "resolve" | "retry" | "accept" | "continue" | "change" | "stop";

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
function AgentNotes({ notes, onComment }: { notes: AgentNote[]; onComment?: (text: string) => void }) {
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
            {onComment && <> <button type="button" className="linkish add-comment" onClick={() => onComment(`${NOTE_LABEL[n.kind]}: ${n.text}`)}>Add as comment</button></>}
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
  const [retire, setRetire] = useState<string[]>([]);
  const [nextReview, setNextReview] = useState("");
  const [reason, setReason] = useState("");
  const [override, setOverride] = useState("");
  const [remember, setRemember] = useState(false);
  const [lesson, setLesson] = useState("");
  const [comments, setComments] = useState<LineComment[]>([]);
  const [ux, setUx] = useState<boolean | null>(null);
  const [docView, setDocView] = useState<"read" | "comment">("read");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    opened.current = Date.now();
    setMode(null); setFeedback(""); setBudget(""); setDays(""); setContent(item.content ?? ""); setErr(""); setPicked(null);
    setReason(""); setRemember(false); setLesson(""); setOverride(""); setRetire([]); setNextReview(item.next_review ?? "");
    setComments([]); setDocView("read"); setUx(null);
  }, [item.interrupt_id, item.content]);

  async function send(decision: string, extra: Record<string, unknown> = {}) {
    setBusy(true);
    setErr("");
    // Time spent with the item open counts as the human's active minutes (capped at two hours).
    const minutes = Math.min(120, Math.round(((Date.now() - opened.current) / 60000) * 10) / 10);
    try {
      await api.decide(featureId, item.interrupt_id, {
        decision, feedback, active_minutes: minutes, ...(reason ? { reason } : {}),
        ...(decision === "revise" && comments.length ? { comments } : {}),
        ...(remember ? { remember: true, ...(lesson.trim() ? { lesson: lesson.trim() } : {}) } : {}), ...extra,
      });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function addNote(text: string) {
    setComments((cs) => (cs.some((c) => c.text === text) ? cs : [...cs, { quote: "", text }]));
  }

  const gate = item.gate === "release"
    ? (item.model === "launch"
      ? { name: "Release", question: "Launch these features together?", approve: "Launch" }
      : { name: "Release", question: "Ship this release train?", approve: "Approve release" })
    : item.gate ? GATES[item.gate] : undefined;
  const fitCheck = item.phase === "fit";
  const incidentGate = item.gate === "incident" || item.gate === "postmortem";
  const inceptionGate = item.gate === "brief" || item.gate === "product";
  const reviewGate = item.gate === "review";
  const roadmapGate = item.gate === "g0" || item.gate === "release" || incidentGate || inceptionGate || reviewGate;
  const stopLabel = item.gate === "g0" ? "Drop initiative" : item.gate === "release" ? "Cancel release"
    : inceptionGate ? "Stop this product" : "Stop feature";
  const chosen = picked ?? (item.actions ?? []).map((a) => a.id);
  const heading =
    item.type === "escalation" && item.phase === "delivery" ? (DELIVERY_FAILED[item.wait ?? ""] ?? "Delivery failed")
    : item.type === "escalation" && item.phase === "security" ? "Security findings block this change"
    : item.type === "escalation" && item.phase === "provisioning" ? "Setting up a new repository failed"
    : fitCheck ? "The design and the architecture don't fit together"
    : item.type === "escalation" ? `${PHASES[item.phase ?? ""] ?? item.phase} needs a person`
    : item.type === "handback" ? `You took over ${gateLabel(item.gate)}`
    : reviewGate ? "Is this initiative moving its goal?"
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
              : `${gateLabel(item.gate)}, decided by ${item.approvers.join(" or ")}.`}
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

      {item.type === "gate" && !reviewGate && item.gate !== "incident" && <DecisionBrief featureId={featureId} interruptId={item.interrupt_id} />}

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

      {fitCheck && item.fit && (
        <div>
          <h3>What doesn't fit</h3>
          <ul className="findings">{item.fit.map((f) => (
            <li key={f.text}><span className="chip plain">{f.to === "design" ? "Design should change" : "Architecture should change"}</span> {f.text}</li>
          ))}</ul>
          <p className="small muted">Both were approved separately; this was checked before planning starts. Send one back with these as its feedback, or carry on and say why.</p>
        </div>
      )}

      {item.ux_design && (
        <div className="ux-choice">
          <h3>UX design</h3>
          <label className="check">
            <input type="checkbox" checked={ux ?? item.ux_design.runs} disabled={!item.can_decide} onChange={(e) => setUx(e.target.checked)} />
            <span>Design the screens (UX design and its approval run before planning)</span>
          </label>
          <p className="small muted">{(ux ?? item.ux_design.runs) ? "Runs: the feature changes what people see." : "Skipped: nothing people see changes."}
            {item.ux_design.why ? ` ${item.ux_design.why}.` : ""}</p>
        </div>
      )}

      {!security && !fitCheck && item.findings && item.findings.length > 0 && (
        <div>
          <h3>{delivery ? "What went wrong" : "What the evaluators found"}</h3>
          <ul className="findings">{item.findings.map((f) => (
            <li key={f}>{f}{item.can_decide && canRevise && item.type === "gate" && (
              <> <button type="button" className="linkish add-comment" onClick={() => addNote(f)}>Add as comment</button></>)}</li>
          ))}</ul>
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
          <div className="evidence md"><Markdown remarkPlugins={[remarkGfm]}>{item.spec_preview}</Markdown></div>
        </details>
      )}

      {item.agent_notes && item.agent_notes.length > 0 && (
        <AgentNotes notes={item.agent_notes} onComment={item.can_decide && canRevise && item.type === "gate" ? addNote : undefined} />
      )}

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
          {item.evidence && (<><h3>Evidence</h3><div className="md small"><Markdown remarkPlugins={[remarkGfm]}>{item.evidence}</Markdown></div></>)}
        </div>
      )}

      {roadmapGate && item.preview && (
        <div>
          {item.gate === "product" && item.monthly_usd && Object.keys(item.monthly_usd).length > 0 && (
            <p className="small" style={{ marginBottom: 8 }}>AWS resources, per month (planning figures from the platform catalog):{" "}
              {Object.entries(item.monthly_usd).map(([env, usd]) => `${env} ${money(usd)}`).join(", ")}.</p>
          )}
          <h3 style={{ marginBottom: 8 }}>{item.gate === "g0" ? "Initiative draft" : item.gate === "incident" ? "Triage"
            : item.gate === "postmortem" ? "Postmortem" : item.gate === "brief" ? "Product brief"
            : item.gate === "product" ? "Product architecture and roadmap" : reviewGate ? "The evidence" : "Release notes"}</h3>
          <div className="evidence md"><Markdown remarkPlugins={[remarkGfm]}>{item.preview}</Markdown></div>
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

      {item.instrumentation && item.instrumentation.events.length > 0 && (
        <div>
          <h3>Goal measurement</h3>
          {!item.instrumentation.checked ? (
            <p className="muted small">Not checked: {item.instrumentation.why}</p>
          ) : (
            <>
              {item.instrumentation.missing.length > 0 && (
                <p className="small">
                  Some events never fired in {item.instrumentation.environment}. The goals they feed can't be
                  measured after release until they do.
                </p>
              )}
              <ul className="plain-list small">
                {item.instrumentation.events.map((e) => {
                  const n = item.instrumentation!.fired[e.event] ?? 0;
                  return (
                    <li key={e.event}>
                      <span className={`chip ${n > 0 ? "go" : "stop"}`}>
                        {n > 0 ? `Fired ${n} time${n === 1 ? "" : "s"}` : "Never fired"}
                      </span>{" "}
                      <strong>{e.event}</strong>
                      <span className="muted"> · feeds {e.goals.join(", ") || "no goal"}{e.repo ? ` · ${e.repo.split("/").pop()}` : ""}</span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
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
          <div className="doc-head">
            <h3>{ARTIFACTS[kind] ?? kind}</h3>
            {item.type === "gate" && item.can_decide && canRevise && (
              <div className="segmented small" role="tablist" aria-label="How to view the document">
                <button type="button" role="tab" aria-selected={docView === "read"} className={docView === "read" ? "on" : ""} onClick={() => setDocView("read")}>Read</button>
                <button type="button" role="tab" aria-selected={docView === "comment"} className={docView === "comment" ? "on" : ""} onClick={() => setDocView("comment")}>Comment on lines</button>
              </div>
            )}
          </div>
          <div className="evidence">{docView === "comment"
            ? <CommentableDoc featureId={featureId} kind={kind} comments={comments} onAdd={(c) => setComments([...comments, c])} />
            : <ArtifactBody featureId={featureId} kind={kind} />}</div>
        </div>
      )}

      {comments.length > 0 && (
        <div className="comments-box">
          <h3>Your comments ({comments.length})</h3>
          <ol className="plain-ol small">
            {comments.map((c, i) => (
              <li key={i}>
                {c.quote && <span className="muted">On “{c.quote.slice(0, 80)}{c.quote.length > 80 ? "…" : ""}”: </span>}{c.text}{" "}
                <button type="button" className="linkish" onClick={() => setComments(comments.filter((_, j) => j !== i))}>Remove</button>
              </li>
            ))}
          </ol>
          <p className="small faint">They go to the agents with “Request changes”, each quoting its line.</p>
        </div>
      )}

      {!item.can_decide ? (
        <p className="note">Waiting on {item.approvers.join(", ") || "the listed approvers"}. You can follow along, but only they can decide.</p>
      ) : (
        <>
          {mode === "revise" || mode === "retry" || mode === "abort" ? (
            <label className="field">
              {mode === "revise" ? (item.gate === "incident" ? "What should triage look at?" : comments.length ? "Anything else to tell the agents? (optional: your comments go too)" : "What should the agents change?") : mode === "retry" ? "Guidance for the next attempt (optional)" : item.gate === "incident" ? "Why is no action needed?" : item.gate === "g0" ? "Why drop this initiative?" : inceptionGate ? "Why stop this product?" : item.gate === "release" ? "Why cancel this release?" : "Why stop this feature?"}
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
          {mode === "resolve" && fitCheck && (
            <label className="field">Why carry on with them as they are? (recorded on the feature)
              <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} autoFocus />
            </label>
          )}
          {mode === "resolve" && !fitCheck && (
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
          {reviewGate && mode === "continue" && (
            <label className="field">Next review
              <input type="date" value={nextReview} onChange={(e) => setNextReview(e.target.value)} />
            </label>
          )}
          {reviewGate && (mode === "continue" || mode === "change" || mode === "stop") && (
            <label className="field">{mode === "change" ? "What should change? (it becomes the new intent's request)" : "Why (recorded on the initiative)"}
              <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} autoFocus={mode === "change"} />
            </label>
          )}
          {reviewGate && mode === "stop" && (item.retire_candidates ?? []).length > 0 && (
            <fieldset className="field">
              <legend>Retire these features too (each opens a retire intent)</legend>
              {item.retire_candidates!.map((r) => (
                <label key={r.feature_id} className="check">
                  <input type="checkbox" checked={retire.includes(r.feature_id)}
                    onChange={(e) => setRetire(e.target.checked ? [...retire, r.feature_id] : retire.filter((x) => x !== r.feature_id))} />
                  <span><strong>{r.feature_id}</strong> {r.title} <span className="small muted">{r.why}</span></span>
                </label>
              ))}
            </fieldset>
          )}
          {learnable && ((item.type === "handback" && mode === null) || mode === "revise" || mode === "retry" || mode === "resolve") && (
            <RememberField on={remember} setOn={setRemember} lesson={lesson} setLesson={setLesson} />
          )}
          {err && <p className="error-note" role="alert">{err}</p>}
          <div className="actions">
            {reviewGate && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => setMode("continue")}>Continue</button>
                <button className="btn secondary" disabled={busy} onClick={() => setMode("change")}>Change course</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("stop")}>Stop the initiative</button>
              </>
            )}
            {reviewGate && mode === "continue" && (
              <button className="btn go" disabled={busy} onClick={() => send("continue", nextReview ? { review_by: nextReview } : {})}>
                Continue to {nextReview || "the next review"}</button>
            )}
            {reviewGate && mode === "change" && (
              <button className="btn" disabled={busy || !feedback.trim()} onClick={() => send("change")}>Open the change as an intent</button>
            )}
            {reviewGate && mode === "stop" && (
              <button className="btn danger" disabled={busy} onClick={() => send("stop", { retire })}>
                {retire.length ? `Stop and retire ${retire.length} feature${retire.length === 1 ? "" : "s"}` : "Stop, keep its features"}</button>
            )}
            {item.type === "gate" && !reviewGate && mode === null && (
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
                    : item.traceability?.blocks ? { override_reason: override.trim() }
                    : item.ux_design && ux !== null && ux !== item.ux_design.runs ? { ui: ux } : {})}>
                  {item.gate === "incident" ? `Approve ${chosen.length} action${chosen.length === 1 ? "" : "s"}` : gate?.approve ?? "Approve"}</button>
                {item.gate !== "release" && canRevise && <button className="btn secondary" disabled={busy} onClick={() => setMode("revise")}>
                  {item.gate === "incident" ? "Look again" : item.gate === "postmortem" ? "Rewrite"
                    : comments.length ? `Request changes (${comments.length} comment${comments.length === 1 ? "" : "s"})` : "Request changes"}</button>}
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
            {fitCheck && mode === null && (
              <>
                <button className="btn" disabled={busy} onClick={() => send("design")}>Send the design back</button>
                <button className="btn secondary" disabled={busy} onClick={() => send("architecture")}>Send the architecture back</button>
                <button className="btn secondary" disabled={busy} onClick={() => setMode("resolve")}>Carry on anyway</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {item.type === "escalation" && !delivery && !security && !fitCheck && mode === null && (
              <>
                <button className="btn go" disabled={busy} onClick={() => setMode("resolve")}>{item.content ? "Finish it myself" : "Mark resolved"}</button>
                <button className="btn secondary" disabled={busy} onClick={() => setMode("retry")}>Let agents retry</button>
                <span className="spacer" />
                <button className="btn danger" disabled={busy} onClick={() => setMode("abort")}>Stop feature</button>
              </>
            )}
            {mode === "revise" && (
              <button className="btn" disabled={busy || (!feedback.trim() && comments.length === 0)} onClick={() => send("revise")}>
                {comments.length ? `Revise with ${comments.length} comment${comments.length === 1 ? "" : "s"}` : "Send back with changes"}</button>
            )}
            {mode === "retry" && (
              <button className="btn" disabled={busy} onClick={() => send("retry", budget ? { budget_usd: Number(budget) } : {})}>Retry with agents</button>
            )}
            {mode === "resolve" && fitCheck && (
              <button className="btn go" disabled={busy || !feedback.trim()} onClick={() => send("resolved")}>Carry on to planning</button>
            )}
            {mode === "resolve" && !fitCheck && (
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
