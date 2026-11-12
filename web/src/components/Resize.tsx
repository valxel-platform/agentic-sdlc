import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type PendingItem } from "../api";
import { threadPath } from "../vocab";

/** While a feature waits at G1, or an initiative at G0, it can change size: what was written carries
 * over and this one stops, saying where it went. */
export function Resize({ id, kind, pending }: { id: string; kind: "feature" | "initiative"; pending: PendingItem[] }) {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  if (!pending.some((p) => p.gate === (kind === "feature" ? "g1" : "g0"))) return null;
  async function go() {
    setBusy(true);
    setErr("");
    try {
      const r = await api.resize(id);
      nav(threadPath(r.feature_id));
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  }
  return (
    <p className="small resize">
      {kind === "feature" ? "Bigger than one feature?" : "Smaller than it looked?"}{" "}
      <button type="button" className="linkish" disabled={busy} onClick={go}>
        {kind === "feature" ? "Make this an initiative" : "Make it one feature"}
      </button>
      <span className="faint"> Nothing written is lost; this one stops and links to the new one.</span>
      {err && <span className="error-note"> {err}</span>}
    </p>
  );
}
