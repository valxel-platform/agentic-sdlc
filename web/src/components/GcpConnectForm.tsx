import { useState } from "react";
import { api } from "../api";

const GCP_REGIONS = ["us-central1", "us-east1", "us-east4", "us-west1", "europe-west1", "asia-south1"];
const PROJECT = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;

/**
 * Google Cloud's two-step connect (ADR 0010): run the connect template in Cloud Shell with this
 * workspace's values, then Valxel acts as the service accounts it made and checks they are this
 * workspace's. The counterpart of the AWS stack-then-check flow.
 */
export function GcpConnectForm({
  ws,
  onDone,
  onError,
}: {
  ws: string;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [mode, setMode] = useState("lite");
  const [region, setRegion] = useState("us-central1");
  const [project, setProject] = useState("");
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  async function open() {
    try {
      const r = await api.gcpLaunch(ws, mode, region);
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
      const c = await api.gcpCheck(ws, project.trim().toLowerCase(), mode);
      onDone(`Connected Google Cloud project ${c.account}.`);
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
            <option value="full">Full: GKE clusters and managed services</option>
          </select>
        </label>
        <label className="field">
          Region
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            {GCP_REGIONS.map((r) => (
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
          <p className="small">Run this in Cloud Shell, with your project ID in place of YOUR_PROJECT_ID:</p>
          <pre className="mono small" aria-label="Connect command">
            {command}
          </pre>
        </div>
      )}
      <form onSubmit={check} className="submit-row">
        <label className="field">
          Your Google Cloud project ID
          <input
            value={project}
            onChange={(e) => setProject(e.target.value)}
            placeholder="acme-products-prod"
            className="mono"
            required
          />
        </label>
        <button
          className={command ? "btn" : "btn secondary"}
          type="submit"
          disabled={busy || !PROJECT.test(project.trim().toLowerCase())}
        >
          {busy ? "Checking…" : "Check the connection"}
        </button>
      </form>
    </div>
  );
}
