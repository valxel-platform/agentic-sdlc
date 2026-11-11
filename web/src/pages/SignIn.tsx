import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { Mark } from "../components/Mark";

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [cfg, setCfg] = useState<{ mode: string; teams?: string[] } | null>(null);
  const [login, setLogin] = useState("");
  const [teams, setTeams] = useState<string[]>([]);
  const [err, setErr] = useState(new URLSearchParams(location.search).get("signin") === "failed"
    ? "GitHub sign-in was cancelled or failed. Try again." : "");

  useEffect(() => { api.authConfig().then(setCfg).catch((e) => setErr(e.message)); }, []);

  async function dev(e: FormEvent) {
    e.preventDefault();
    try {
      await api.devLogin(login.trim(), teams, []);
      onSignedIn();
    } catch (e2) { setErr((e2 as Error).message); }
  }

  return (
    <div className="signin">
      <section className="signin-art" aria-label="About Vrixel SDLC">
        <div className="brand"><Mark size={32} /> Vrixel SDLC</div>
        <Line />
        <p>Agents carry each feature from intent to production. You clear the signals: every gate waits
          for the people your GitHub teams name.</p>
      </section>
      <div className="card">
        <div>
          <h1>Sign in</h1>
          <p className="muted" style={{ marginTop: 8 }}>Your GitHub teams decide which gates you can approve.</p>
        </div>
        {err && <p className="error-note" role="alert">{err}</p>}
        {!cfg && !err && <p className="faint">Checking how to sign in…</p>}
        {cfg?.mode === "github" && <a className="btn" href="/api/auth/login">Sign in with GitHub</a>}
        {cfg?.mode === "dev" && (
          <form onSubmit={dev} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <p className="note">Development sign-in: choose any identity. Not for production.</p>
            <label className="field">GitHub login
              <input value={login} onChange={(e) => setLogin(e.target.value)} required autoFocus placeholder="your-login" />
            </label>
            <fieldset className="field">
              <legend className="field">Teams to act as</legend>
              <div className="team-list">
                {(cfg.teams ?? []).map((t) => (
                  <label key={t}>
                    <input type="checkbox" checked={teams.includes(t)}
                      onChange={(e) => setTeams(e.target.checked ? [...teams, t] : teams.filter((x) => x !== t))} />
                    {t}
                  </label>
                ))}
              </div>
            </fieldset>
            <button className="btn" type="submit" disabled={!login.trim()}>Sign in</button>
          </form>
        )}
      </div>
    </div>
  );
}

/** The line from intent to production: track sections and the six gate signals, the first cleared,
 * the next at caution. */
function Line() {
  const gates = [0, 1, 2, 3, 4, 5];
  return (
    <svg className="line" viewBox="0 0 640 150" aria-hidden="true">
      <line x1="10" y1="104" x2="630" y2="104" stroke="#2c3d35" strokeWidth="3" />
      <line x1="10" y1="104" x2="250" y2="104" stroke="#d7e2db" strokeWidth="5" />
      <rect x="236" y="98" width="22" height="12" rx="2" fill="#5c9ee3" />
      {gates.map((g) => {
        const x = 70 + g * 100;
        const lamp = g === 0 || g === 1 ? "#4cb67a" : g === 2 ? "#f2a93b" : "#24332c";
        return (
          <g key={g}>
            <line x1={x} y1="104" x2={x} y2="62" stroke="#4b5f55" strokeWidth="3" />
            <rect x={x - 11} y="22" width="22" height="44" rx="11" fill="#0a1410" stroke="#4b5f55" />
            <circle cx={x} cy="35" r="6" fill={g < 2 ? lamp : "#24332c"} />
            <circle cx={x} cy="53" r="6" fill={g === 2 ? lamp : "#24332c"} />
            <text x={x} y="132" textAnchor="middle" fill="#8fa398" fontFamily="IBM Plex Mono, monospace" fontSize="13">G{g + 1}</text>
          </g>
        );
      })}
    </svg>
  );
}
