import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Trace, type TracePr } from "../api";
import { threadPath } from "../vocab";

const RESULT: Record<string, string> = { passed: "passed", failed: "failed", skipped: "skipped", missing: "not run" };

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

/** From the feature's reason to production: each acceptance criterion with the goal it serves, the
 * task and test that cover it, its pull requests, its acceptance results and where it runs, plus
 * the gaps in that chain and production incidents on its components. */
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
        <table className="quality">
          <caption className="visually-hidden">Each acceptance criterion from task to production</caption>
          <thead>
            <tr><th scope="col">Criterion</th><th scope="col">Task and test</th><th scope="col">Pull requests</th>
              <th scope="col">Acceptance</th><th scope="col">Running in</th></tr>
          </thead>
          <tbody>
            {t.criteria.map((c) => (
              <tr key={c.id}>
                <th scope="row" title={c.text}>{c.id}</th>
                <td>{c.tasks.length ? `${c.tasks.join(", ")}${c.test ? ` / ${c.test}` : ""}` : <strong>no task</strong>}</td>
                <td><Prs prs={c.prs} /></td>
                <td>{Object.keys(c.acceptance).length
                  ? Object.entries(c.acceptance).map(([st, r]) => `${st}: ${RESULT[r] ?? r}`).join(", ")
                  : <span className="faint">not run</span>}</td>
                <td>{Object.keys(c.deployed).length
                  ? Object.entries(c.deployed).map(([repo, env]) => `${repo.split("/").pop()} in ${env}`).join(", ")
                  : <span className="faint">not deployed</span>}</td>
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
