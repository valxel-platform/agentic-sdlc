import { type AgentSession } from "../api";
import { PHASES, money } from "../vocab";

/** Each agent session: what it worked on, how long it took, what it cost, and its trace. A session
 * that loaded skills beyond its phase's says which, so a reviewer can see what else shaped it. */
export function AgentSessions({ sessions, caption, traced }: { sessions: AgentSession[]; caption: string; traced?: boolean }) {
  if (sessions.length === 0) return null;
  const anyLink = sessions.some((s) => s.url);
  const anyExtra = sessions.some((s) => (s.extra_skills ?? []).length > 0);
  return (
    <div>
      <table className="quality">
        <caption className="small">{caption}</caption>
        <thead>
          <tr><th scope="col">Step</th><th scope="col">Agent</th><th scope="col">Task</th>
            <th scope="col">Time</th><th scope="col">Cost</th>
            {anyExtra && <th scope="col">Other skills it used</th>}<th scope="col">Trace</th></tr>
        </thead>
        <tbody>
          {sessions.map((s, i) => (
            <tr key={s.trace_id || `${s.at}-${i}`}>
              <td>{PHASES[s.phase] ?? s.phase}</td>
              <td>{s.role.replace(/_/g, " ")} <span className="muted small">{s.model.split("/").pop()}</span></td>
              <td>{s.task || "—"}{s.attempt ? <span className="muted small"> try {s.attempt}</span> : null}</td>
              <td>{s.duration_s < 90 ? `${Math.round(s.duration_s)} s` : `${(s.duration_s / 60).toFixed(1)} min`}</td>
              <td>{money(s.cost_usd)}</td>
              {anyExtra && <td>{(s.extra_skills ?? []).length ? (s.extra_skills ?? []).join(", ") : <span className="muted">—</span>}</td>}
              <td>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">Open trace</a> : <span className="muted">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!anyLink && traced !== false && (
        <p className="small muted">Traces aren't linked: set VALXEL_OTEL_ENDPOINT and VALXEL_TRACE_URL to open each session.</p>
      )}
    </div>
  );
}
