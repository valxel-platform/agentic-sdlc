import { useEffect, useRef, useState } from "react";
import { api, type CloudGuide, type IntakeSession } from "../api";

const POLL_MS = 10_000;
const POLL_FOR_MS = 30 * 60_000;
const LABEL: Record<string, string> = { non_production: "Non-production", production: "Production" };
const EXAMPLE: Record<string, { np: string; p: string; region: string }> = {
  aws: { np: "111111111111", p: "222222222222", region: "us-east-1" },
  gcp: { np: "acme-cln-nonprod", p: "acme-cln-prod", region: "us-central1" },
  azure: { np: "1b2c3d4e-…-0001", p: "1b2c3d4e-…-0002", region: "eastus2" },
};

type Connect = { state: "idle" | "opened" | "connected" | "error"; command?: string; note?: string };

/**
 * A new product's two cloud accounts, on the front door's draft (cloud/onboarding.py): the IDs and
 * the region, the cloud's own pages for making them, and a Connect for each that opens the connect
 * template already filled in for that account. Valxel then checks it by itself until it connects.
 */
export function CloudAccounts({
  s,
  ws,
  disabled,
  onSaved,
}: {
  s: IntakeSession;
  ws: string;
  disabled: boolean;
  onSaved: (next: IntakeSession) => void;
}) {
  const cloud = s.cloud ?? "aws";
  const d = s.draft;
  const [np, setNp] = useState(d.cloud_accounts?.non_production ?? "");
  const [p, setP] = useState(d.cloud_accounts?.production ?? "");
  const [region, setRegion] = useState(d.region ?? "");
  const [error, setError] = useState("");
  const [guide, setGuide] = useState<CloudGuide | null>(null);
  const [connect, setConnect] = useState<Record<string, Connect>>({});
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => window.clearInterval(t)), []);
  const accounts = [d.cloud_accounts?.non_production, d.cloud_accounts?.production].filter(Boolean).join(",");
  useEffect(() => {
    if (!ws || d.cloud !== "given") return;
    let live = true;
    api
      .connections(ws)
      .then((rows) => {
        if (!live) return;
        const done = rows.find((r) => r.tool === cloud)?.connections ?? [];
        const ok = Object.fromEntries(
          done
            .filter((c) => c.status === "connected")
            .map((c) => [String(c.account).toLowerCase(), { state: "connected" as const }]),
        );
        setConnect((c) => ({ ...c, ...ok }));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [ws, cloud, accounts, d.cloud]);

  const g = guide;
  const noun = g?.noun ?? { aws: "account", gcp: "project", azure: "subscription" }[cloud];
  const ex = EXAMPLE[cloud];

  async function save(next: { np?: string; p?: string; region?: string }) {
    const a = (next.np ?? np).trim();
    const b = (next.p ?? p).trim();
    const r = (next.region ?? region).trim();
    if (!a || !b || !r) return; // all three, or nothing yet
    try {
      setError("");
      onSaved(await api.intakeDraft(s.id, { cloud: `non-production: ${a}, production: ${b}, region: ${r}` }));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function later(on: boolean) {
    try {
      setError("");
      onSaved(await api.intakeDraft(s.id, { cloud: on ? "later" : "" }));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function showGuide() {
    try {
      setGuide(guide ? null : await api.cloudOnboarding(d.name || "the product"));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function watch(account: string) {
    const started = Date.now();
    const t = window.setInterval(async () => {
      try {
        const r = await api.connectionPoll(ws, cloud, account);
        if (r.status === "connected") {
          window.clearInterval(t);
          setConnect((c) => ({ ...c, [account]: { state: "connected" } }));
        } else if (Date.now() - started > POLL_FOR_MS) {
          window.clearInterval(t);
          setConnect((c) => ({ ...c, [account]: { ...c[account], note: r.why || "Not connected yet" } }));
        }
      } catch {
        window.clearInterval(t);
      }
    }, POLL_MS);
    timers.current.push(t);
  }

  async function open(account: string) {
    const r = region.trim() || ex.region;
    try {
      if (cloud === "aws") {
        const out = await api.awsLaunch(ws, "full", r, account);
        window.open(out.url, "_blank", "noopener");
        setConnect((c) => ({ ...c, [account]: { state: "opened", note: out.sign_in } }));
      } else {
        const out =
          cloud === "gcp"
            ? await api.gcpLaunch(ws, "full", r, account)
            : await api.azureLaunch(ws, "full", r, account);
        window.open(out.url, "_blank", "noopener");
        setConnect((c) => ({ ...c, [account]: { state: "opened", command: out.command } }));
      }
      watch(account);
    } catch (e) {
      setConnect((c) => ({ ...c, [account]: { state: "error", note: (e as Error).message } }));
    }
  }

  const given = d.cloud === "given";
  return (
    <fieldset className="cloud-accounts" aria-label="Cloud accounts">
      <legend>
        Cloud accounts{" "}
        <span className="muted small">
          · runs on {g?.name ?? { aws: "AWS", gcp: "Google Cloud", azure: "Azure" }[cloud]}
        </span>
      </legend>
      <div className="split">
        <label className={`ifield${s.missing.includes("cloud") && !np ? " missing" : ""}`}>
          <span>Non-production {noun}</span>
          <input
            value={np}
            disabled={disabled || d.cloud === "later"}
            placeholder={ex.np}
            onChange={(e) => setNp(e.target.value)}
            onBlur={() => save({ np })}
          />
        </label>
        <label className={`ifield${s.missing.includes("cloud") && !p ? " missing" : ""}`}>
          <span>Production {noun}</span>
          <input
            value={p}
            disabled={disabled || d.cloud === "later"}
            placeholder={ex.p}
            onChange={(e) => setP(e.target.value)}
            onBlur={() => save({ p })}
          />
        </label>
      </div>
      <label className="ifield">
        <span>Region</span>
        <input
          value={region}
          disabled={disabled || d.cloud === "later"}
          placeholder={ex.region}
          onChange={(e) => setRegion(e.target.value)}
          onBlur={() => save({ region })}
        />
      </label>
      {error && (
        <p className="small error-note" role="alert">
          {error}
        </p>
      )}
      <div className="row small">
        <button type="button" className="linkish" onClick={showGuide}>
          {guide ? "Hide how to create them" : `How do I create these ${noun}s?`}
        </button>
        <label className="inline">
          <input
            type="checkbox"
            checked={d.cloud === "later"}
            disabled={disabled}
            onChange={(e) => later(e.target.checked)}
          />{" "}
          Set them up later
        </label>
      </div>
      {guide && (
        <ol className="small guide" aria-label={`How to create ${noun}s`}>
          {guide.steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      )}
      {given && ws && (
        <ul className="connect-list" aria-label="Connect each account">
          {(["non_production", "production"] as const).map((k) => {
            const account = d.cloud_accounts[k] ?? "";
            const c = connect[account] ?? { state: "idle" };
            return (
              <li key={k}>
                <span>
                  {LABEL[k]}: <code>{account}</code>
                </span>
                {c.state === "connected" ? (
                  <span className="chip go">Connected</span>
                ) : (
                  <button
                    type="button"
                    className="btn small"
                    disabled={disabled}
                    onClick={() => open(account)}
                  >
                    {c.state === "opened" ? "Open again" : "Connect"}
                  </button>
                )}
                {c.state === "opened" && <span className="muted small">Waiting for it to connect…</span>}
                {c.note && <p className="small">{c.note}</p>}
                {c.command && (
                  <pre className="mono small" aria-label={`Connect command for ${account}`}>
                    {c.command}
                  </pre>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </fieldset>
  );
}
