import { useState } from "react";
import { api } from "../api";

/** A thread that stopped on an error, and the way to carry it on from its last good step. A gate
 * can't be skipped this way: the thread waits at its next gate as before. */
export function Stopped({
  id,
  error,
  what = "workflow",
  style,
  onResumed,
}: {
  id: string;
  error: string;
  what?: string;
  style?: React.CSSProperties;
  onResumed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function resume() {
    setBusy(true);
    setErr("");
    try {
      await api.resume(id);
      onResumed();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="error-note" role="alert" style={style}>
      <p style={{ margin: 0 }}>
        The {what} stopped with an error: {error}
      </p>
      <p className="small" style={{ margin: "8px 0 0" }}>
        Fix the cause, then resume. It carries on from its last completed step.{" "}
        <button className="btn secondary small-btn" disabled={busy} onClick={resume}>
          {busy ? "Resuming…" : "Resume"}
        </button>
      </p>
      {err && (
        <p className="small" style={{ margin: "8px 0 0" }}>
          Couldn't resume: {err}
        </p>
      )}
    </div>
  );
}
