import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type ChangeRecord, type Lookback, type Me } from "../api";
import { useLive } from "../live";
import { threadPath } from "../vocab";

const TYPE: Record<string, { label: string; chip: string; says: string }> = {
  standard: { label: "Standard", chip: "plain", says: "Went through the gates" },
  emergency: { label: "Emergency", chip: "wait", says: "A hotfix, or on-call acting in an incident" },
  automatic: { label: "Automatic", chip: "run", says: "The tool, under the remediation policy" },
};
const ACTION: Record<string, string> = {
  deploy: "Released", flag_on: "Launched (flags on)", flag_off: "Flag turned off", rollback: "Rolled back",
};

function pct(v: number | null): string {
  return v == null ? "–" : `${Math.round(v * 100)}%`;
}

function minutes(v: number | null): string {
  if (v == null) return "–";
  return v < 90 ? `${Math.round(v)} min` : `${(v / 60).toFixed(1)} h`;
}

function when(at: string): string {
  return new Date(at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function ChangesPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const productId = params.get("product") || me.lanes[0]?.id || "";
  const [rows, setRows] = useState<ChangeRecord[] | null>(null);
  const [lb, setLb] = useState<Lookback | null>(null);
  const [chain, setChain] = useState<{ ok: boolean; records: number; broken_at: string | null } | null>(null);
  const [open, setOpen] = useState<string>("");
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    if (!productId) return;
    Promise.all([api.changes(productId), api.lookback(productId, 6), api.verifyChanges()])
      .then(([c, l, v]) => { setRows(c); setLb(l); setChain(v); setErr(""); })
      .catch((e) => setErr(e.message));
  }, [productId]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Changes</h1>
          <p className="muted">Every change to production, who approved it, and the incidents behind it.</p>
        </div>
        <div className="head-actions">
          {me.lanes.length > 1 && (
            <select aria-label="Product" value={productId} onChange={(e) => setParams({ product: e.target.value })}>
              {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          )}
          <a className="btn secondary" href={`/api/changes/export.csv?product_id=${encodeURIComponent(productId)}`}>Download for audit (CSV)</a>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {chain && !chain.ok && (
        <p className="error-note" role="alert">
          The change log was altered: its records no longer chain together from {chain.broken_at}. Check the database's access log.
        </p>
      )}

      {lb && (
        <section className="panel" aria-labelledby="h-lookback">
          <h2 id="h-lookback" className="section-title">The last six months</h2>
          <div className="table-scroll">
          <table className="quality">
            <caption className="visually-hidden">Incidents and changes by month</caption>
            <thead><tr>
              <th scope="col">Month</th><th scope="col" className="num">Incidents</th>
              <th scope="col" className="num">Fixed by the tool</th><th scope="col" className="num">Needed on-call</th>
              <th scope="col" className="num">Time to recover (median)</th>
              <th scope="col" className="num">Releases</th><th scope="col" className="num">Releases that caused one</th>
            </tr></thead>
            <tbody>
              {lb.months.map((m) => (
                <tr key={m.month}>
                  <th scope="row">{m.month}</th>
                  <td className="num">{m.incidents}{m.by_severity.sev1 ? <span className="small faint"> ({m.by_severity.sev1} SEV1)</span> : null}</td>
                  <td className="num">{m.fixed_by_tool}</td>
                  <td className="num">{m.escalated}</td>
                  <td className="num">{minutes(m.recover_minutes_median)}</td>
                  <td className="num">{m.releases}</td>
                  <td className="num">{m.releases_causing_incidents} <span className="small faint">({pct(m.change_failure_rate)})</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          {lb.causes.length > 0 && (
            <>
              <h3 className="pad" style={{ paddingTop: 12 }}>Releases behind the most incidents</h3>
              <ul className="ext-list">
                {lb.causes.map((c) => (
                  <li key={c.change_id}>
                    <div className="work-line"><strong>{c.summary}</strong> <span className="small faint">{c.change_id}</span></div>
                    <div className="small muted">
                      {c.incidents.map((i, n) => <span key={i}>{n > 0 && ", "}<Link to={threadPath(i)}>{i}</Link></span>)}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      )}

      <section className="panel" aria-labelledby="h-log" style={{ marginTop: 20 }}>
        <div className="panel-head">
          <h2 id="h-log" className="section-title">Change log</h2>
          {chain?.ok && <span className="small muted">{chain.records} records, chain intact</span>}
        </div>
        {rows && rows.length === 0 && <p className="small muted pad">No production changes yet.</p>}
        {rows && rows.length > 0 && (
          <div className="table-scroll">
          <table className="quality">
            <thead><tr>
              <th scope="col">When</th><th scope="col">Change</th><th scope="col">Type</th>
              <th scope="col">Approved by</th><th scope="col">Caused</th>
            </tr></thead>
            <tbody>
              {rows.map((r) => (
                <ChangeRow key={r.id} r={r} open={open === r.id} toggle={() => setOpen(open === r.id ? "" : r.id)} />
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>
    </>
  );
}

function ChangeRow({ r, open, toggle }: { r: ChangeRecord; open: boolean; toggle: () => void }) {
  const t = TYPE[r.type];
  const thread = r.incident_id || r.feature_id || r.release_id;
  return (
    <>
      <tr>
        <td className="small">{when(r.at)}</td>
        <td>
          <button type="button" className="textlink" aria-expanded={open} onClick={toggle}>
            {ACTION[r.action] ?? r.action}: {r.summary}
          </button>
          <div className="small faint">{r.id}{r.outcome === "failed" && <span className="chip stop" style={{ marginLeft: 6 }}>Failed</span>}</div>
        </td>
        <td><span className={`chip ${t.chip}`} title={t.says}>{t.label}</span></td>
        <td className="small">{r.approved_by.join(", ") || "–"}</td>
        <td className="small">
          {r.caused_incidents.length === 0 ? "–" : r.caused_incidents.map((i, n) => (
            <span key={i}>{n > 0 && ", "}<Link to={threadPath(i)}>{i}</Link></span>))}
        </td>
      </tr>
      {open && (
        <tr className="detail-row">
          <td colSpan={5}>
            <dl className="kv">
              <dt>What changed</dt>
              <dd>{r.targets.map((x) => `${x.kind} ${x.name}${x.release ? ` (${x.release})` : ""}${x.on !== undefined ? (x.on ? " on" : " off") : ""}`).join("; ") || "–"}</dd>
              <dt>Asked for by</dt><dd>{r.requested_by || "–"}</dd>
              <dt>Approvals</dt>
              <dd>{r.approvals.length ? r.approvals.map((a) => `${a.gate.toUpperCase()} ${a.approver}`).join(", ") : r.approved_by.join(", ")}</dd>
              {(r.evidence.pull_requests?.length ?? 0) > 0 && (
                <>
                  <dt>Pull requests</dt>
                  <dd>{r.evidence.pull_requests!.map((u, n) => <span key={u}>{n > 0 && ", "}<a href={u} target="_blank" rel="noreferrer">{u.replace(/^https:\/\/github.com\//, "")}</a></span>)}</dd>
                </>
              )}
              {r.evidence.quality && Object.keys(r.evidence.quality).length > 0 && (
                <><dt>Checks</dt><dd>{Object.keys(r.evidence.quality).join(", ")}</dd></>
              )}
              {r.evidence.triage && <><dt>Why</dt><dd>{r.evidence.triage}</dd></>}
              {r.detail && <><dt>What went wrong</dt><dd>{r.detail}</dd></>}
              {thread && <><dt>Record</dt><dd><Link to={threadPath(thread)}>{thread}</Link></dd></>}
              <dt>Hash</dt><dd className="mono small">{r.hash.slice(0, 16)}… (after {r.prev_hash.slice(0, 8)}…)</dd>
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}
