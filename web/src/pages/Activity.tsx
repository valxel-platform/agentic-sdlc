import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type AuditEntry, type ChangeRecord, type Me } from "../api";
import { useLive } from "../live";
import { threadPath } from "../vocab";

type Row = { at: string; text: string; to?: string; kind: string };

/**
 * Activity — composed client-side from existing logs only: the product change
 * log (changes()) and the workspace audit log (workspaceAudit(), admin-only).
 * There is no global activity endpoint. Entries are sealed: they cannot be
 * edited or deleted, including by admins.
 */
export function ActivityPage({ me }: { me: Me }) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [err, setErr] = useState("");
  const [auditDenied, setAuditDenied] = useState(false);
  const productId = me.lanes[0]?.id ?? "";
  const ws = Object.keys(me.roles ?? {})[0] ?? "";

  const load = useCallback(() => {
    const jobs: Promise<void>[] = [];
    if (productId) {
      jobs.push(
        api
          .changes(productId)
          .then((cs: ChangeRecord[]) => {
            setRows((cur) =>
              merge(
                cur,
                cs.map((c) => ({
                  at: c.at,
                  kind: "Change",
                  text: `${c.summary} (${c.kind}, ${c.env})`,
                  to: c.feature_id ? threadPath(c.feature_id) : undefined,
                })),
              ),
            );
          })
          .catch((e) => setErr(e.message)),
      );
    }
    if (ws) {
      jobs.push(
        api
          .workspaceAudit(ws)
          .then((as: AuditEntry[]) => {
            setRows((cur) =>
              merge(
                cur,
                as.map((a) => ({
                  at: a.at,
                  kind: "Workspace",
                  text: `${a.actor} ${a.action}${a.target ? ` — ${a.target}` : ""}`,
                  to: undefined,
                })),
              ),
            );
          })
          .catch(() => setAuditDenied(true)),
      );
    }
    if (jobs.length === 0) setRows([]);
  }, [productId, ws]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  if (err)
    return (
      <p className="error-note" role="alert">
        {err}
      </p>
    );
  if (!rows) return <p className="faint">Loading…</p>;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Activity</h1>
          <p className="muted">The newest changes and workspace events, in one place.</p>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="panel empty">
          <h2>Nothing here yet</h2>
          <p>Changes and workspace events will appear here as they happen.</p>
        </div>
      ) : (
        <section className="panel" aria-label="Activity">
          <ul className="activity-list pad">
            {rows.map((r, i) => (
              <li key={i}>
                <span className="small muted mono">{r.at.slice(0, 16).replace("T", " ")}</span>
                <span>
                  <span className="chip plain">{r.kind}</span>{" "}
                  {r.to ? <Link to={r.to}>{r.text}</Link> : r.text}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {auditDenied && (
        <p className="small muted pad">The workspace audit log is only visible to workspace admins.</p>
      )}
      <p className="sealed-note">
        Sealed history: entries are hash-chained and cannot be edited or deleted, including by admins.
      </p>
    </>
  );
}

function merge(cur: Row[] | null, add: Row[]): Row[] {
  const all = [...(cur ?? []), ...add];
  all.sort((a, b) => b.at.localeCompare(a.at));
  return all;
}
