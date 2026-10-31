import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";

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
      <div className="panel card">
        <div className="brand">
          <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#18212B" /><path d="M16 7l7 9-7 9-7-9z" fill="#E2A43B" /></svg>
          Vrixel SDLC
        </div>
        <div>
          <h1 style={{ fontSize: 24 }}>Sign in to review and steer features</h1>
          <p className="muted" style={{ marginTop: 8 }}>Your GitHub teams decide which gates you can approve.</p>
        </div>
        {err && <p className="error-note" role="alert">{err}</p>}
        {cfg?.mode === "github" && <a className="btn" href="/api/auth/login">Sign in with GitHub</a>}
        {cfg?.mode === "dev" && (
          <form onSubmit={dev} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <p className="note">Development sign-in: choose any identity. Not for production.</p>
            <label className="field">GitHub login
              <input value={login} onChange={(e) => setLogin(e.target.value)} required autoFocus placeholder="your-login" />
            </label>
            <fieldset style={{ border: 0, padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
              <legend className="field" style={{ fontWeight: 600, fontSize: 13, color: "var(--ink-2)", marginBottom: 6 }}>Teams to act as</legend>
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
