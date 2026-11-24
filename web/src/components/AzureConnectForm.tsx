import { useState } from "react";
import { api } from "../api";

const AZURE_REGIONS = [
  "eastus",
  "eastus2",
  "westus2",
  "centralus",
  "westeurope",
  "northeurope",
  "centralindia",
];
const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Azure's two-step connect (ADR 0010): deploy the connect template from Cloud Shell with this
 * workspace's values, then Valxel signs in as the two identities it made (federated credentials,
 * no secrets) and checks they are this workspace's. The template prints the IDs to paste here.
 */
export function AzureConnectForm({
  ws,
  onDone,
  onError,
}: {
  ws: string;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [mode, setMode] = useState("lite");
  const [region, setRegion] = useState("eastus");
  const [ids, setIds] = useState({ subscription: "", deploy: "", agents: "" });
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const clean = (s: string) => s.trim().toLowerCase();
  const ready =
    GUID.test(clean(ids.subscription)) && GUID.test(clean(ids.deploy)) && GUID.test(clean(ids.agents));
  async function open() {
    try {
      const r = await api.azureLaunch(ws, mode, region);
      setCommand(r.command);
      window.open(r.url, "_blank", "noopener");
    } catch (e) {
      onError((e as Error).message);
    }
  }
  async function check(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const c = await api.azureCheck(ws, {
        subscription: clean(ids.subscription),
        deploy_client_id: clean(ids.deploy),
        agents_client_id: clean(ids.agents),
        mode,
      });
      onDone(`Connected Azure subscription ${c.account}.`);
    } catch (ex) {
      onError((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const field = (key: keyof typeof ids, label: string) => (
    <label className="field">
      {label}
      <input
        value={ids[key]}
        onChange={(e) => setIds({ ...ids, [key]: e.target.value })}
        placeholder="00000000-0000-0000-0000-000000000000"
        className="mono"
        required
      />
    </label>
  );
  return (
    <div className="stack">
      <div className="grid-2">
        <label className="field">
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="lite">Lite: products run on VMs</option>
            <option value="full">Full: AKS and managed services</option>
          </select>
        </label>
        <label className="field">
          Region
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            {AZURE_REGIONS.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
      </div>
      <p>
        <button className="btn" type="button" onClick={open}>
          Open Cloud Shell with the connect template
        </button>
      </p>
      {command && (
        <div className="stack">
          <p className="small">
            Run this in Cloud Shell, signed in to the subscription you're connecting. It prints the IDs to
            paste below:
          </p>
          <pre className="mono small" aria-label="Connect command">
            {command}
          </pre>
        </div>
      )}
      <form onSubmit={check} className="stack">
        {field("subscription", "Subscription ID")}
        <div className="grid-2">
          {field("deploy", "Deploy client ID")}
          {field("agents", "Agents client ID")}
        </div>
        <p className="submit-row">
          <button className={command ? "btn" : "btn secondary"} type="submit" disabled={busy || !ready}>
            {busy ? "Checking…" : "Check the connection"}
          </button>
        </p>
      </form>
    </div>
  );
}
