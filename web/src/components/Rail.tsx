import type { Station } from "../api";
import { GATES, PHASES } from "../vocab";

const STATE_TEXT: Record<string, string> = {
  done: "done", skipped: "skipped (low risk)", waiting: "waiting on a person", current: "agents working",
  stopped: "stopped", todo: "not started",
};

function stationName(s: Station): string {
  return s.kind === "gate" ? `${s.key.toUpperCase()} ${GATES[s.key]?.name ?? ""}` : PHASES[s.key] ?? s.key;
}

/** A feature drawn as a train on a line: phases are track sections (lit while agents work in
 * them), gates are signals a person clears (amber while waiting, green once cleared). */
export function Rail({ stations, compact = false }: { stations: Station[]; compact?: boolean }) {
  if (!stations.length) return <div className="faint small">Starting…</div>;
  const summary = stations.map((s) => `${stationName(s)}: ${STATE_TEXT[s.state]}`).join("; ");
  return (
    <div className={`rail ${compact ? "compact" : "full"}`} role="img" aria-label={summary}>
      {stations.map((s) => (
        <div key={s.key} className={`station ${s.kind} ${s.state}`} title={`${stationName(s)}: ${STATE_TEXT[s.state]}`}>
          <span className="track" />
          <span className="mark">{s.kind === "gate" ? <span className="diamond" /> : <span className="dot" />}</span>
          <span className="label">{s.kind === "gate" ? s.key.toUpperCase() : PHASES[s.key]}</span>
        </div>
      ))}
    </div>
  );
}
