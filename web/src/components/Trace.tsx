import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Trace, type TraceCase, type TracePr } from "../api";
import { threadPath } from "../vocab";

const RESULT: Record<string, string> = { passed: "passed", failed: "failed", skipped: "skipped", missing: "not run" };
const CASE: Record<TraceCase["status"], string> = {
  passed: "passed", failed: "failed", written: "written, not merged", missing: "test not on the branch",
  pending: "not run yet", manual: "manual", unknown: "no checkable test",
};

function Cases({ cases }: { cases: TraceCase[] }) {
  if (cases.length === 0) return <strong>no test case</strong>;
  return (
    <ul className="plain-list small">
      {cases.map((c) => (
        <li key={c.id} title={`${c.what}\n${c.ref}`}>
          {c.id} ({c.kind}): {c.status === "passed" ? CASE[c.status] : <strong>{CASE[c.status] ?? c.status}</strong>}
        </li>
      ))}
    </ul>
  );
}

function Prs({ prs }: { prs: TracePr[] }) {
  if (prs.length === 0) return <span className="faint">none yet</span>;
  return (
    <>
      {prs.map((p, i) => (
        <span key={p.key}>
          {i > 0 && ", "}
          {p.url ? <a href={p.url} target="_blank" rel="noreferrer">{p.key.split("/").pop()}</a> : p.key.split("/").pop()}
        </span>
      ))}
    </>
  );
}

/** From the feature's reason to production: each requirement (acceptance criteria and NFRs) with the
 * goal it serves, its tasks, its test cases and their results, its pull requests, its acceptance
 * results and where it runs, plus the gaps in that chain and production incidents on its components. */
export function TracePanel({ featureId, version }: { featureId: string; version: number }) {
  const [t, setT] = useState<Trace | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api.trace(featureId).then((x) => { setT(x); setErr(""); }).catch((e: Error) => setErr(e.message));
  }, [featureId, version]);
  if (err) return null;
  if (!t || (t.criteria.length === 0 && t.goals.length === 0)) return null;
  return (
    <section className="panel" aria-labelledby="h-trace">
      <h2 id="h-trace" className="section-title">Traceability</h2>
      <div style={{ padding: "12px 16px 16px" }}>
        {t.goals.length > 0 && (
          <p className="small">
            {t.goals.map((g) => (
              <span key={g.id} style={{ marginRight: 12 }}>
                <strong>{g.id}</strong> {g.text}
                {g.metrics.length === 0 && <span className="muted"> (no metric)</span>}
              </span>
            ))}
          </p>
        )}
        <p className="small">
          {t.complete ? "Every requirement is traced to passing tests." : `${t.gaps.length} gap${t.gaps.length === 1 ? "" : "s"} below.`}{" "}
          <a href={api.traceCsvUrl(featureId)} download>Export the matrix (CSV)</a>
        </p>
        <table className="quality">
          <caption className="visually-hidden">Each requirement from goal to test and production</caption>
          <thead>
            <tr><th scope="col">Requirement</th><th scope="col">Goal</th><th scope="col">Tasks</th><th scope="col">Test cases</th>
              <th scope="col">Pull requests</th><th scope="col">Acceptance</th><th scope="col">Running in</th></tr>
          </thead>
          <tbody>
            {[...t.criteria, ...(t.nfrs ?? [])].map((c) => (
              <tr key={c.id}>
                <th scope="row" title={c.text}>{c.id}{c.type !== "functional" && <span className="faint"> {c.type}</span>}</th>
                <td>{c.goals.join(", ") || <span className="faint">none</span>}</td>
                {c.deferred !== null ? (
                  <td colSpan={5}><span className="muted">Deferred: {c.deferred}</span></td>
                ) : (
                  <>
                    <td>{c.tasks.length ? c.tasks.join(", ") : <strong>no task</strong>}</td>
                    <td><Cases cases={c.cases} /></td>
                    <td><Prs prs={c.prs} /></td>
                    <td>{Object.keys(c.acceptance).length
                      ? Object.entries(c.acceptance).map(([st, r]) => `${st}: ${RESULT[r] ?? r}`).join(", ")
                      : <span className="faint">not run</span>}</td>
                    <td>{Object.keys(c.deployed).length
                      ? Object.entries(c.deployed).map(([repo, env]) => `${repo.split("/").pop()} in ${env}`).join(", ")
                      : <span className="faint">not deployed</span>}</td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        {t.decisions.length > 0 && (
          <p className="small muted">Decisions: {t.decisions.map((d) => `${d.id} ${d.text}`).join("; ")}</p>
        )}
        {t.gaps.length > 0 && (
          <div className="note" role="status">
            <strong>Gaps</strong>
            <ul className="plain-list small">{t.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
          </div>
        )}
        {t.incidents.length > 0 && (
          <p className="small">
            Incidents on its components:{" "}
            {t.incidents.map((i, n) => (
              <span key={i.id}>{n > 0 && ", "}<Link to={threadPath(i.id)}>{i.id}</Link> {i.title} ({i.status})</span>
            ))}
          </p>
        )}
      </div>
    </section>
  );
}
