import type { Station } from "../api";
import { GATES, PHASES } from "../vocab";

const STATE_TEXT: Record<string, string> = {
  done: "done", skipped: "skipped (low risk)", waiting: "waiting on a person", current: "agents working",
  stopped: "stopped", todo: "not started",
};

function stationName(s: Station): string {
  return s.kind === "gate" ? `${s.key.toUpperCase()} ${GATES[s.key]?.name ?? ""}` : PHASES[s.key] ?? s.key;
}

/** A feature drawn as a train on a line: phase stations, with gate signals between them. */
export function Rail({ stations, compact = false }: { stations: Station[]; compact?: boolean }) {
  if (!stations.length) return <div className="faint small">Starting…</div>;
  let last = -1;
  stations.forEach((s, i) => { if (s.state === "done" || s.state === "skipped") last = i; });
  const progress = last < 0 ? 0 : ((last + 0.5) / stations.length) * 100;
  const summary = stations.map((s) => `${stationName(s)}: ${STATE_TEXT[s.state]}`).join("; ");
  return (
    <div className={`rail ${compact ? "compact" : "full"}`} role="img" aria-label={summary}>
      <div className="seg-line" style={{ left: `${50 / stations.length}%`, right: `${50 / stations.length}%` }} />
      <div className="progress" style={{ left: `${50 / stations.length}%`, width: `${Math.max(0, progress - 50 / stations.length)}%` }} />
      {stations.map((s) => (
        <div key={s.key} className={`station ${s.kind} ${s.state}`} title={`${stationName(s)}: ${STATE_TEXT[s.state]}`}>
          <span className="mark">{s.kind === "gate" ? <span className="diamond" /> : <span className="dot" />}</span>
          <span className="label">{s.kind === "gate" ? s.key.toUpperCase() : PHASES[s.key]}</span>
        </div>
      ))}
    </div>
  );
}
