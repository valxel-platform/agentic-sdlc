import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type HealthMeasure, type Me, type SafeModeStatus, type ToolHealth } from "../api";
import { useLive } from "../live";
import { money, threadPath } from "../vocab";

const ALL = "all";
const PHASE: Record<string, string> = {
  discovery: "Discovery",
  spec: "Spec",
  design: "UX design",
  architecture: "Architecture",
  plan: "Plan",
  build: "Build",
  ci: "CI",
};
const GATE: Record<string, string> = {
  g1: "G1 brief",
  g2: "G2 spec",
  g3: "G3 UX design",
  g4: "G4 architecture",
  g5: "G5 code",
  g6: "G6 release",
};
const CHECK: Record<string, string> = {
  replay: "Replay",
  health: "Health since the fix",
  drift: "Weekly drift run",
};

function pct(v: number | null | undefined): string {
  return v == null ? "–" : `${Math.round(v * 100)}%`;
}

function num(v: number | null | undefined): string {
  return v == null ? "–" : String(v);
}

/** "now (was)": the last window against the baseline before it. */
function Cell({
  now,
  was,
  fmt = pct,
}: {
  now?: number | null;
  was?: number | null;
  fmt?: (v?: number | null) => string;
}) {
  return (
    <td className="num">
      {fmt(now)} <span className="small faint">({fmt(was)})</span>
    </td>
  );
}

function keys(
  a: Record<string, unknown> | undefined,
  b: Record<string, unknown> | undefined,
  order: string[],
) {
  const all = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
  return [...order.filter((k) => all.has(k)), ...[...all].filter((k) => !order.includes(k)).sort()];
}

function Agents({ now, was }: { now: HealthMeasure; was: HealthMeasure }) {
  const phases = keys(now.phases, was.phases, Object.keys(PHASE));
  const gates = keys(now.gates, was.gates, Object.keys(GATE));
  return (
    <>
      <section className="panel" aria-labelledby="h-phases" style={{ marginBottom: 20 }}>
        <h2 id="h-phases" className="section-title">
          Agents by phase
        </h2>
        {phases.length === 0 ? (
          <p className="small muted" style={{ padding: 16 }}>
            No runs in this window or the baseline yet.
          </p>
        ) : (
          <div className="table-scroll">
            <table className="quality">
              <caption className="small muted">Last window, with the baseline in brackets</caption>
              <thead>
                <tr>
                  <th scope="col">Phase</th>
                  <th scope="col" className="num">
                    Runs
                  </th>
                  <th scope="col" className="num">
                    Critic passed, people sent back
                  </th>
                  <th scope="col" className="num">
                    Critic failed, people kept
                  </th>
                  <th scope="col" className="num">
                    Escalated
                  </th>
                  <th scope="col" className="num">
                    Stalled
                  </th>
                  <th scope="col" className="num">
                    Loops (mean)
                  </th>
                </tr>
              </thead>
              <tbody>
                {phases.map((p) => (
                  <tr key={p}>
                    <th scope="row">{PHASE[p] ?? p}</th>
                    <Cell now={now.phases[p]?.runs} was={was.phases[p]?.runs} fmt={num} />
                    <Cell now={now.critic[p]?.false_pass} was={was.critic[p]?.false_pass} />
                    <Cell now={now.critic[p]?.false_fail} was={was.critic[p]?.false_fail} />
                    <Cell now={now.phases[p]?.escalation_rate} was={was.phases[p]?.escalation_rate} />
                    <Cell now={now.phases[p]?.stall_rate} was={was.phases[p]?.stall_rate} />
                    <Cell now={now.phases[p]?.loops_mean} was={was.phases[p]?.loops_mean} fmt={num} />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <div className="grid-2">
        <section className="panel" aria-labelledby="h-gates">
          <h2 id="h-gates" className="section-title">
            Approved first time
          </h2>
          {gates.length === 0 ? (
            <p className="small muted" style={{ padding: 16 }}>
              No gate decisions yet.
            </p>
          ) : (
            <table className="quality">
              <thead>
                <tr>
                  <th scope="col">Gate</th>
                  <th scope="col" className="num">
                    Decided
                  </th>
                  <th scope="col" className="num">
                    First time
                  </th>
                </tr>
              </thead>
              <tbody>
                {gates.map((g) => (
                  <tr key={g}>
                    <th scope="row">{GATE[g] ?? g}</th>
                    <Cell now={now.gates[g]?.decided} was={was.gates[g]?.decided} fmt={num} />
                    <Cell now={now.gates[g]?.first_time_approval} was={was.gates[g]?.first_time_approval} />
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        <section className="panel" aria-labelledby="h-spend">
          <h2 id="h-spend" className="section-title">
            Spend and escalations
          </h2>
          <dl className="kv" style={{ padding: "8px 16px 16px" }}>
            <dt>Model spend per feature</dt>
            <dd>
              {now.cost_per_feature.mean == null ? "–" : money(now.cost_per_feature.mean)}{" "}
              <span className="small faint">
                ({was.cost_per_feature.mean == null ? "–" : money(was.cost_per_feature.mean)})
              </span>
            </dd>
            <dt>Why work went to a person</dt>
            <dd>
              {Object.keys(now.escalation_reasons).length === 0
                ? "No escalations"
                : Object.entries(now.escalation_reasons)
                    .map(([k, n]) => `${k} (${n})`)
                    .join("; ")}
            </dd>
          </dl>
        </section>
      </div>
    </>
  );
}

function SafeModePanel({
  productId,
  s,
  owner,
  onChanged,
}: {
  productId: string;
  s: SafeModeStatus;
  owner: boolean;
  onChanged: () => void;
}) {
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  const act = (p: Promise<unknown>, done: string) =>
    p
      .then(() => {
        setMsg(done);
        setNote("");
        onChanged();
      })
      .catch((e) => setMsg(e.message));
  return (
    <section className="safe-mode" aria-labelledby={`h-safe-${productId}`}>
      <h2 id={`h-safe-${productId}`} className="section-title">
        {productId} has been in safe mode for {s.hours} hours
      </h2>
      <p>{s.says}</p>
      <p className="small">
        {s.fix ? `Fix: ${s.fix.note} (${s.fix.by}). ` : "No fix marked yet. "}
        {s.normal_checks} of {s.needed} normal checks in a row.
        {s.ready && " Ready for an owner to end it."}
      </p>
      {s.checks.length > 0 && (
        <ul className="small">
          {s.checks.map((c) => (
            <li key={c.at + c.source}>
              {CHECK[c.source] ?? c.source}, {c.at.slice(0, 16).replace("T", " ")}:{" "}
              {c.normal === null ? "not counted" : c.normal ? "normal" : "not normal"}. {c.says}
            </li>
          ))}
        </ul>
      )}
      {s.approvals_to_review.length > 0 && (
        <p className="small">
          Approved while things were going wrong:{" "}
          {s.approvals_to_review.map((a, i) => (
            <span key={a.feature_id + a.gate}>
              {i > 0 && ", "}
              <Link to={threadPath(a.feature_id)}>{a.feature_id}</Link> {a.gate.toUpperCase()}
            </span>
          ))}
        </p>
      )}
      {owner && (
        <div className="head-actions" style={{ marginTop: 8 }}>
          <input
            aria-label="What was changed"
            placeholder="What was changed"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            className="btn secondary"
            disabled={!note.trim()}
            onClick={() => act(api.safeModeFixed(productId, note.trim()), "Fix recorded")}
          >
            Mark fixed
          </button>
          <button
            className="btn secondary"
            onClick={() =>
              act(api.safeModeReplay(productId), "Replay started: the result shows here as a check")
            }
          >
            Replay the tripped phases
          </button>
          <button
            className="btn"
            disabled={!s.ready}
            onClick={() => act(api.safeModeLeave(productId), "Safe mode ended")}
          >
            End safe mode
          </button>
        </div>
      )}
      {msg && (
        <p className="small" role="status">
          {msg}
        </p>
      )}
    </section>
  );
}

/** How Valxel's own agents are doing on real work, beside a product's business metrics (Pilot,
 * Costs): the critic against people, first-time approvals, escalations, stalls and spend, against
 * their baseline; what's alerting and what changed; and safe mode. The tool's owners also see every
 * product together, the queue and the weekly drift evals. */
export function HealthPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const owner = !!me.tool_owner;
  const productId = params.get("product") || me.lanes[0]?.id || (owner ? ALL : "");
  const [h, setH] = useState<ToolHealth | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    if (!productId) return;
    api
      .toolHealth(productId === ALL ? undefined : productId)
      .then((r) => {
        setH(r);
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, [productId]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;
  const all = productId === ALL;
  const safe: Record<string, SafeModeStatus> = !h?.safe_mode
    ? {}
    : all
      ? (h.safe_mode as Record<string, SafeModeStatus>)
      : { [productId]: h.safe_mode as SafeModeStatus };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Agent health</h1>
          <p className="muted">
            How the agents are doing on {all ? "every product's" : "this product's"} work over the last{" "}
            {h?.window_days ?? 7} days, against the {h?.baseline_days ?? 28} days before.
          </p>
        </div>
        {(me.lanes.length > 1 || owner) && (
          <div className="head-actions">
            <select
              aria-label="Product"
              value={productId}
              onChange={(e) => setParams({ product: e.target.value })}
            >
              {owner && <option value={ALL}>All products</option>}
              {me.lanes.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      {h && (
        <>
          {Object.entries(safe).map(([pid, s]) => (
            <SafeModePanel key={pid} productId={pid} s={s} owner={owner} onChanged={load} />
          ))}
          <section className="panel" aria-labelledby="h-alerts" style={{ marginBottom: 20 }}>
            <h2 id="h-alerts" className="section-title">
              Alerts
            </h2>
            {h.alerts.length === 0 ? (
              <p className="small muted" style={{ padding: 16 }}>
                Nothing is alerting: the agents are within their thresholds and their own baseline.
              </p>
            ) : (
              <ul style={{ padding: "0 16px 16px 32px" }}>
                {h.alerts.map((a) => (
                  <li key={a.key}>{a.says}</li>
                ))}
              </ul>
            )}
            {h.what_changed_summary && (
              <p className="small" style={{ padding: "0 16px 16px" }}>
                {h.what_changed_summary}
              </p>
            )}
          </section>
          <Agents now={h.current} was={h.baseline} />
          {all && (
            <section className="panel" aria-labelledby="h-ops" style={{ marginTop: 20 }}>
              <h2 id="h-ops" className="section-title">
                Queue and drift
              </h2>
              <dl className="kv" style={{ padding: "8px 16px 16px" }}>
                <dt>Run queue</dt>
                <dd>
                  {h.queue
                    ? `${h.queue.queued} queued, ${h.queue.running} running, oldest ${Math.round(h.queue.oldest_s / 60)} minutes`
                    : "Runs in process (no queue)"}
                </dd>
                <dt>Runs the queue gave up on</dt>
                <dd>
                  {h.dead_letters?.count ? (
                    <>
                      {h.dead_letters.count}:{" "}
                      {h.dead_letters.recent.map((d, i) => (
                        <span key={d.thread_id + d.at}>
                          {i > 0 && ", "}
                          <Link to={threadPath(d.thread_id)}>{d.thread_id}</Link>
                        </span>
                      ))}
                    </>
                  ) : (
                    "None"
                  )}
                </dd>
                <dt>Weekly drift evals</dt>
                <dd>
                  {!h.drift
                    ? "Not run yet"
                    : h.drift.regressions.length
                      ? `Worse on ${h.drift.at.slice(0, 10)}: ${h.drift.regressions.slice(0, 3).join("; ")}`
                      : `At their baseline on ${h.drift.at.slice(0, 10)} (${h.drift.anchor_cases} pinned cases)`}
                </dd>
              </dl>
            </section>
          )}
        </>
      )}
    </>
  );
}
