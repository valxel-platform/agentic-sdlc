import { useEffect, useRef, useState } from "react";
import { api, type CloudGuide, type IntakeSession, type WorkspaceClouds } from "../api";

const POLL_MS = 10_000;
const POLL_FOR_MS = 30 * 60_000;
const LABEL: Record<string, string> = { non_production: "Non-production", production: "Production" };
const NAME: Record<string, string> = { aws: "AWS", gcp: "Google Cloud", azure: "Azure" };
const NOUN: Record<string, string> = { aws: "account", gcp: "project", azure: "subscription" };
const EXAMPLE: Record<string, { np: string; p: string; region: string }> = {
  aws: { np: "111111111111", p: "222222222222", region: "us-east-1" },
  gcp: { np: "acme-cln-nonprod", p: "acme-cln-prod", region: "us-central1" },
  azure: { np: "1b2c3d4e-…-0001", p: "1b2c3d4e-…-0002", region: "eastus2" },
};

type Cloud = "aws" | "gcp" | "azure";
type Connect = { state: "idle" | "opened" | "connected" | "error"; command?: string; note?: string };
export interface CloudValue {
  cloud: "" | "given" | "later" | "shared";
  cloud_accounts: { non_production?: string; production?: string };
  region: string;
}

/**
 * Two cloud accounts (non-production and production) and their region (cloud/onboarding.py): a new
 * product's own, or a workspace's shared ones. The cloud's own pages for making them, and a Connect
 * for each that opens the connect template already filled in for that account; Valxel then checks
 * it by itself until it connects. For a new product in a workspace with shared accounts, the first
 * choice is whether to share the workspace's clusters (the default, and cheapest) or have its own.
 */
export function CloudAccounts({
  cloud,
  ws,
  value,
  productName,
  disabled,
  onSave,
  sharedIn = "",
  allowLater = true,
  missing = false,
}: {
  cloud: Cloud;
  ws: string;
  value: CloudValue;
  productName: string;
  disabled: boolean;
  /** "later", "shared", "dedicated", "" or "non-production: <id>, production: <id>, region: <region>" */
  onSave: (text: string) => Promise<void>;
  /** The workspace whose shared clusters this product may use, when it has them. */
  sharedIn?: string;
  allowLater?: boolean;
  missing?: boolean;
}) {
  const [np, setNp] = useState(value.cloud_accounts?.non_production ?? "");
  const [p, setP] = useState(value.cloud_accounts?.production ?? "");
  const [region, setRegion] = useState(value.region ?? "");
  const [own, setOwn] = useState(!sharedIn || value.cloud === "given");
  const [error, setError] = useState("");
  const [guide, setGuide] = useState<CloudGuide | null>(null);
  const [connect, setConnect] = useState<Record<string, Connect>>({});
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => window.clearInterval(t)), []);
  const given = value.cloud === "given";
  const accounts = [value.cloud_accounts?.non_production, value.cloud_accounts?.production]
    .filter(Boolean)
    .join(",");
  useEffect(() => {
    if (!ws || !given) return;
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
  }, [ws, cloud, accounts, given]);

  const noun = NOUN[cloud];
  const ex = EXAMPLE[cloud];

  async function send(text: string) {
    try {
      setError("");
      await onSave(text);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function save(next: { np?: string; p?: string; region?: string }) {
    const a = (next.np ?? np).trim();
    const b = (next.p ?? p).trim();
    const r = (next.region ?? region).trim();
    if (!a || !b || !r) return; // all three, or nothing yet
    void send(`non-production: ${a}, production: ${b}, region: ${r}`);
  }

  async function showGuide() {
    try {
      setGuide(guide ? null : await api.cloudOnboarding(productName || "the product"));
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

  const later = value.cloud === "later";
  return (
    <fieldset className="cloud-accounts" aria-label="Cloud accounts">
      <legend>
        Cloud accounts <span className="muted small">· runs on {guide?.name ?? NAME[cloud]}</span>
      </legend>
      {sharedIn && (
        <div className="row small" role="radiogroup" aria-label="Where it runs">
          <label className="inline">
            <input
              type="radio"
              name="placement"
              checked={!own}
              disabled={disabled}
              onChange={() => {
                setOwn(false);
                void send("shared");
              }}
            />{" "}
            Share {sharedIn}'s clusters (cheapest)
          </label>
          <label className="inline">
            <input
              type="radio"
              name="placement"
              checked={own}
              disabled={disabled}
              onChange={() => {
                setOwn(true);
                void send("dedicated");
              }}
            />{" "}
            Its own {noun}s
          </label>
        </div>
      )}
      {sharedIn && !own && (
        <p className="small muted">
          It runs on {sharedIn}'s two clusters, in its own namespace with its own cloud identities. A product
          with PHI or payments data always gets its own {noun}s: the product gate asks for them if its brief
          finds such data.
        </p>
      )}
      {own && (
        <>
          <div className="split">
            <label className={`ifield${missing && !np ? " missing" : ""}`}>
              <span>Non-production {noun}</span>
              <input
                value={np}
                disabled={disabled || later}
                placeholder={ex.np}
                onChange={(e) => setNp(e.target.value)}
                onBlur={() => save({ np })}
              />
            </label>
            <label className={`ifield${missing && !p ? " missing" : ""}`}>
              <span>Production {noun}</span>
              <input
                value={p}
                disabled={disabled || later}
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
              disabled={disabled || later}
              placeholder={ex.region}
              onChange={(e) => setRegion(e.target.value)}
              onBlur={() => save({ region })}
            />
          </label>
          <div className="row small">
            <button type="button" className="linkish" onClick={showGuide}>
              {guide ? "Hide how to create them" : `How do I create these ${noun}s?`}
            </button>
            {allowLater && (
              <label className="inline">
                <input
                  type="checkbox"
                  checked={later}
                  disabled={disabled}
                  onChange={(e) => void send(e.target.checked ? "later" : "")}
                />{" "}
                Set them up later
              </label>
            )}
          </div>
        </>
      )}
      {error && (
        <p className="small error-note" role="alert">
          {error}
        </p>
      )}
      {guide && (
        <ol className="small guide" aria-label={`How to create ${noun}s`}>
          {guide.steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      )}
      {own && given && ws && (
        <ul className="connect-list" aria-label="Connect each account">
          {(["non_production", "production"] as const).map((k) => {
            const account = value.cloud_accounts[k] ?? "";
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

/** The new-product draft's box: a product's own accounts, or its workspace's shared clusters. */
export function DraftCloudAccounts({
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
  const [sharedIn, setSharedIn] = useState("");
  useEffect(() => {
    if (!s.workspace) return;
    let live = true;
    api
      .workspaceCloudAccounts(s.workspace)
      .then((r) => {
        if (live) setSharedIn(r.accounts ? s.workspace || "" : "");
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [s.workspace]);
  return (
    <CloudAccounts
      key={sharedIn}
      cloud={s.cloud ?? "aws"}
      ws={ws}
      value={s.draft}
      productName={s.draft.name}
      disabled={disabled}
      missing={s.missing.includes("cloud")}
      sharedIn={sharedIn}
      onSave={async (text) => onSaved(await api.intakeDraft(s.id, { cloud: text }))}
    />
  );
}

/** A workspace's two shared accounts, set by its admins: where every product that shares runs. */
export function WorkspaceCloudAccounts({ ws, admin }: { ws: string; admin: boolean }) {
  const [v, setV] = useState<WorkspaceClouds | null>(null);
  useEffect(() => {
    api
      .workspaceCloudAccounts(ws)
      .then(setV)
      .catch(() => undefined);
  }, [ws]);
  if (!v) return null;
  return (
    <section className="panel" aria-labelledby="h-shared-cloud">
      <h2 id="h-shared-cloud" className="section-title">
        Shared {v.noun}s
      </h2>
      <p className="small muted">
        Products that share run on this workspace's two clusters ({v.planes.non_production} and{" "}
        {v.planes.production}), each in its own namespace: one pair of cluster fees for all of them. Products
        with PHI or payments data always get their own.
      </p>
      <CloudAccounts
        key={v.accounts ? "set" : "unset"}
        cloud={v.cloud}
        ws={ws}
        value={{ cloud: v.accounts ? "given" : "", cloud_accounts: v.accounts ?? {}, region: v.region ?? "" }}
        productName="your products"
        disabled={!admin}
        allowLater={false}
        onSave={async (text) => setV(await api.setWorkspaceCloudAccounts(ws, text))}
      />
    </section>
  );
}
