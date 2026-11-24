import { useCallback, useEffect, useState } from "react";
import { AzureConnectForm } from "../components/AzureConnectForm";
import { GcpConnectForm } from "../components/GcpConnectForm";
import { api, type Me, type ToolRow } from "../api";

const NEVER: string[] = [
  "See or store your passwords",
  "Change who can approve a gate",
  "Read anything outside what you choose below",
];

const REGIONS = ["us-east-1", "us-east-2", "us-west-2", "ap-south-1", "eu-west-1"];

/**
 * Connected Apps — the REAL tools only (GitHub, GitHub repo-creation, AWS,
 * Jira, Lucid, Sentry, PagerDuty). Slack, Figma and Vercel do not exist in
 * the backend and are not presented as connectable. Wired to the same
 * connections() / disconnect() / awsLaunch() / awsCheck() data as the
 * Workspace Connections tab. Connect is the same full-page redirect to the
 * vendor's own install/sign-in page; the scope you pick here is what you
 * confirm on that page.
 */
export function ConnectedAppsPage({ me }: { me: Me }) {
  const workspaces = Object.keys(me.roles ?? {});
  const [ws, setWs] = useState(workspaces[0] ?? "");
  const [rows, setRows] = useState<ToolRow[] | null>(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [modal, setModal] = useState<ToolRow | null>(null);
  const admin = me.roles?.[ws] === "owner" || me.roles?.[ws] === "admin";

  const load = useCallback(() => {
    if (!ws) return;
    api
      .connections(ws)
      .then((r) => {
        setRows(r);
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, [ws]);
  useEffect(load, [load]);

  function disconnect(tool: string, account: string, name: string) {
    if (
      !window.confirm(
        `Disconnect ${name} for ${account}? Features that need it will wait until it's connected again.`,
      )
    )
      return;
    api
      .disconnect(ws, tool, account)
      .then((r) => {
        setNote(`Disconnected. To remove Valxel's access on ${name}'s side too: ${r.revoke}.`);
        load();
      })
      .catch((e) => setErr(e.message));
  }

  if (!ws) {
    return (
      <div className="panel empty">
        <h2>No workspace yet</h2>
        <p>Set up a workspace first, then connect the tools it uses.</p>
      </div>
    );
  }
  const connected = (rows ?? []).filter((t) => t.status === "connected").length;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Connected apps</h1>
          <p className="muted">
            The tools Valxel works with. Connecting always happens on the app&rsquo;s own sign-in, with the
            exact scope shown — Valxel never sees or stores your password, and disconnecting revokes access
            immediately.
          </p>
        </div>
        {workspaces.length > 1 && (
          <label className="field">
            Workspace
            <select value={ws} onChange={(e) => setWs(e.target.value)}>
              {workspaces.map((w) => (
                <option key={w} value={w}>
                  {w}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      {note && (
        <p className="note" aria-live="polite">
          {note}
        </p>
      )}
      {rows && (
        <p className="small muted" style={{ marginBottom: 12 }}>
          {connected} of {rows.length} apps connected
        </p>
      )}
      {!rows && !err && <p className="faint">Loading…</p>}
      <div className="app-grid">
        {(rows ?? []).map((t) => {
          const chip = t.status === "connected" ? "go" : t.status === "needs_attention" ? "wait" : "plain";
          const label =
            t.status === "connected"
              ? "Connected"
              : t.status === "needs_attention"
                ? "Needs attention"
                : "Not connected";
          return (
            <section key={t.tool} className="panel app-card" aria-labelledby={`h-app-${t.tool}`}>
              <h2 id={`h-app-${t.tool}`}>
                {t.name} <span className={`chip ${chip}`}>{label}</span>
              </h2>
              <p className="small muted grow">{t.grants}</p>
              {t.connections.map((c) => (
                <div key={c.account} className="small">
                  <strong>{c.account}</strong>
                  <span className="muted"> · connected by {c.connected_by}</span>
                  {c.status === "needs_attention" && <p className="error-note">{c.last_error}</p>}
                  {admin && (
                    <p>
                      <button className="small-btn" onClick={() => disconnect(t.tool, c.account, t.name)}>
                        Disconnect
                      </button>
                    </p>
                  )}
                </div>
              ))}
              {!t.available && (
                <p className="small error-note">
                  It isn&rsquo;t set up on this Valxel yet: whoever runs Valxel adds its OAuth client.
                </p>
              )}
              {t.tool === "aws" && admin && t.available && (
                <AwsConnectForm
                  ws={ws}
                  onDone={(m) => {
                    setNote(m);
                    setErr("");
                    load();
                  }}
                  onError={setErr}
                />
              )}
              {t.tool === "gcp" && admin && t.available && (
                <GcpConnectForm
                  ws={ws}
                  onDone={(m) => {
                    setNote(m);
                    setErr("");
                    load();
                  }}
                  onError={setErr}
                />
              )}
              {t.tool === "azure" && admin && t.available && (
                <AzureConnectForm
                  ws={ws}
                  onDone={(m) => {
                    setNote(m);
                    setErr("");
                    load();
                  }}
                  onError={setErr}
                />
              )}
              {t.tool !== "aws" && t.tool !== "gcp" && t.tool !== "azure" && admin && t.available && (
                <p>
                  <button
                    className={t.status === "connected" ? "btn secondary" : "btn"}
                    onClick={() => setModal(t)}
                  >
                    {t.status === "not_connected"
                      ? "Connect"
                      : t.status === "needs_attention"
                        ? "Reconnect"
                        : "Manage"}
                  </button>
                </p>
              )}
              {!admin && <p className="small muted">Only workspace admins connect or disconnect apps.</p>}
            </section>
          );
        })}
      </div>
      {modal && <ConnectModal tool={modal} ws={ws} onClose={() => setModal(null)} />}
    </>
  );
}

function ConnectModal({ tool, ws, onClose }: { tool: ToolRow; ws: string; onClose: () => void }) {
  const [step, setStep] = useState(0);
  const [scope, setScope] = useState("all");
  const install = `/api/workspaces/${encodeURIComponent(ws)}/connections/${tool.tool}/install`;
  return (
    <div className="modal-overlay" role="dialog" aria-label={`Connect ${tool.name}`} onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <ol className="wizard-steps-narrow" aria-label="Connect steps">
          {["What it can do", "Choose the scope", "Approve"].map((s, i) => (
            <li key={s} className={i === step ? "current" : ""}>
              {i + 1}. {s}
            </li>
          ))}
        </ol>
        <h2>Connect {tool.name}</h2>
        {step === 0 && (
          <>
            <h3 className="section-title small" style={{ marginTop: 12 }}>
              What it can do
            </h3>
            <p className="small">{tool.grants}</p>
            <p className="small muted">Asked for at: {tool.asked_when}.</p>
            <h3 className="section-title small" style={{ marginTop: 12 }}>
              What it can never do
            </h3>
            <ul className="perm-list small">
              {NEVER.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          </>
        )}
        {step === 1 && (
          <>
            <h3 className="section-title small" style={{ marginTop: 12 }}>
              Choose the scope
            </h3>
            <label className="radio small">
              <input
                type="radio"
                name="scope"
                value="all"
                checked={scope === "all"}
                onChange={() => setScope("all")}
              />
              <span>Everything it can reach — the widest access {tool.name} offers</span>
            </label>
            <label className="radio small">
              <input
                type="radio"
                name="scope"
                value="selected"
                checked={scope === "selected"}
                onChange={() => setScope("selected")}
              />
              <span>Only what you choose — pick the accounts, repos or sites on the next screen</span>
            </label>
            <p className="small muted" style={{ marginTop: 8 }}>
              You confirm {scope === "all" ? "the widest access" : "only what you choose"} on {tool.name}
              &rsquo;s own sign-in page. Valxel asks for nothing more than this.
            </p>
          </>
        )}
        {step === 2 && (
          <>
            <p className="small">
              Approve on {tool.name}&rsquo;s own sign-in. It will show you the exact scope —{" "}
              {scope === "all" ? "everything it can reach" : "only what you choose"} — before you agree.
              Valxel never sees your password.
            </p>
            {tool.connections.length > 0 && (
              <p className="small muted">
                Already connected: {tool.connections.map((c) => c.account).join(", ")}. This adds another
                account.
              </p>
            )}
          </>
        )}
        <div className="submit-row" style={{ marginTop: 16 }}>
          {step > 0 && (
            <button className="btn secondary" onClick={() => setStep(step - 1)}>
              Back
            </button>
          )}
          {step < 2 && (
            <button className="btn" onClick={() => setStep(step + 1)}>
              Continue
            </button>
          )}
          {step === 2 && (
            <button
              className="btn"
              onClick={() => {
                window.location.href = install;
              }}
            >
              Continue to {tool.name}
            </button>
          )}
          <button className="btn secondary" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

/** AWS keeps its own two-step flow (CloudFormation stack, then account check), as in the Workspace tab. */
function AwsConnectForm({
  ws,
  onDone,
  onError,
}: {
  ws: string;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [mode, setMode] = useState("lite");
  const [region, setRegion] = useState("us-east-1");
  const [account, setAccount] = useState("");
  const [opened, setOpened] = useState(false);
  const [busy, setBusy] = useState(false);
  async function open() {
    try {
      const r = await api.awsLaunch(ws, mode, region);
      window.open(r.url, "_blank", "noopener");
      setOpened(true);
    } catch (e) {
      onError((e as Error).message);
    }
  }
  async function check(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const c = await api.awsCheck(ws, account, mode);
      onDone(`Connected AWS account ${c.account}.`);
    } catch (ex) {
      onError((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="stack">
      <div className="grid-2">
        <label className="field">
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="lite">Lite: products run on VMs</option>
            <option value="full">Full: EKS clusters and managed services</option>
          </select>
        </label>
        <label className="field">
          Region
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            {REGIONS.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
      </div>
      <p>
        <button className="btn" type="button" onClick={open}>
          Open AWS to create the stack
        </button>
      </p>
      <form onSubmit={check} className="submit-row">
        <label className="field">
          Your AWS account ID
          <input
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            placeholder="123456789012"
            className="mono"
            inputMode="numeric"
            required
          />
        </label>
        <button
          className={opened ? "btn" : "btn secondary"}
          type="submit"
          disabled={busy || account.replace(/-/g, "").length !== 12}
        >
          {busy ? "Checking…" : "Check the connection"}
        </button>
      </form>
    </div>
  );
}
