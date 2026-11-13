import { useEffect, useState } from "react";
import { api, type FeaturePath, type NotifyPrefs } from "../api";
import { GATES, PHASES } from "../vocab";

export function aboutTime(minutes: number): string {
  if (minutes < 90) return `about ${Math.max(5, Math.round(minutes / 5) * 5)} min`;
  if (minutes < 36 * 60) return `about ${Math.round(minutes / 60)} h`;
  return `about ${Math.round(minutes / 1440)} days`;
}

function when(iso: string): string {
  const d = new Date(iso);
  const days = (d.getTime() - Date.now()) / 86400000;
  const day = days < 1 && d.getDate() === new Date().getDate() ? "today"
    : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return days < 2 ? `${day}, ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}` : day;
}

const STATE: Record<string, string> = {
  done: "Done", skipped: "Skipped", waiting: "Waiting", current: "Agents working", stopped: "Stopped", todo: "",
};

/** Where the work is, how long each step usually takes here, and which steps need you. */
export function PathPanel({ path }: { path: FeaturePath }) {
  const next = path.stations.find((s) => s.state === "waiting" || s.state === "current");
  const mineAhead = path.stations.filter((s) => s.mine && (s.state === "todo" || s.state === "waiting"));
  return (
    <section className="panel path-panel" aria-labelledby="path-h">
      <div className="path-head">
        <h2 id="path-h" className="section-title">The path</h2>
        {path.expected_done ? (
          <p>Expected to reach everyone <strong>{when(path.expected_done)}</strong>
            {mineAhead.length > 0 && <span className="muted"> if you decide {mineAhead.length === 1 ? "your step" : `your ${mineAhead.length} steps`} on time</span>}</p>
        ) : <p className="muted">{path.stations.some((s) => s.state === "stopped") ? "Stopped." : "Done."}</p>}
      </div>
      <ol className="path-steps">
        {path.stations.filter((s) => s.state !== "skipped").map((s) => (
          <li key={s.key} className={`${s.state}${s.mine ? " mine" : ""}${s === next ? " now" : ""}`}>
            <span className="path-name">{s.kind === "gate" ? GATES[s.key]?.name ?? s.key.toUpperCase() : PHASES[s.key] ?? s.key}</span>
            <span className="path-meta small">
              {s.mine && s.state !== "done" && <span className="chip wait">{s.state === "waiting" ? "Needs you now" : "Needs you"}</span>}
              {STATE[s.state] && <span>{STATE[s.state]}</span>}
              {(s.state === "todo" || s.state === "waiting" || s.state === "current") && (
                <span className="faint" title={s.from_history ? "From this product's history" : "A default until this product has history"}>
                  {s.kind === "gate" ? "people usually take " : "usually "}{aboutTime(s.eta_minutes)}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
      <p className="small faint">Estimates come from this product's past features where there are enough; otherwise defaults.</p>
    </section>
  );
}

/** "Tell me when I'm needed": email, chat, and whether to hear when watched work reaches staging. */
export function NotifyPanel({ threadId, watching }: { threadId: string; watching: boolean }) {
  const [p, setP] = useState<NotifyPrefs | null>(null);
  const [w, setW] = useState(watching);
  const [email, setEmail] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => { api.notifications().then((x) => { setP(x); setEmail(x.email); }).catch(() => undefined); }, []);
  useEffect(() => setW(watching), [watching]);
  if (!p) return null;
  const save = (b: Parameters<typeof api.setNotifications>[0]) =>
    api.setNotifications(b).then((x) => { setP(x); setErr(""); }).catch((e) => setErr((e as Error).message));
  return (
    <section className="panel notify-panel" aria-labelledby="notify-h">
      <h2 id="notify-h" className="section-title">Tell me when I'm needed</h2>
      <label className="check small"><input type="checkbox" checked={p.needed} onChange={(e) => save({ needed: e.target.checked })} />
        When a decision waits for me (any feature)</label>
      <label className="check small"><input type="checkbox" checked={w} onChange={(e) => api.watch(threadId, e.target.checked).then((r) => setW(r.watching))} />
        Watch this one</label>
      <label className="check small"><input type="checkbox" checked={p.staging} disabled={!w} onChange={(e) => save({ staging: e.target.checked })} />
        Also when what I watch reaches staging</label>
      <label className="field small">Email
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} onBlur={() => email !== p.email && save({ email })}
          placeholder="you@example.com" />
      </label>
      {!p.channels.email && !p.channels.chat && <p className="small faint">No email or chat channel is set up yet: ask whoever runs the tool to set VALXEL_SMTP_HOST or VALXEL_NOTIFY_WEBHOOK.</p>}
      {p.channels.chat && <p className="small faint">Your team's chat channel is told too.</p>}
      {err && <p className="error-note small" role="alert">{err}</p>}
    </section>
  );
}
