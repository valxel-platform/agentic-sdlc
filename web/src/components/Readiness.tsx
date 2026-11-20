import { Link } from "react-router-dom";
import type { ReleaseReadiness } from "../api";
import { threadPath } from "../vocab";

const GROUPS: [keyof ReleaseReadiness["counts"], string][] = [
  ["ready", "Ready"],
  ["checking", "Being checked"],
  ["building", "Building"],
  ["design", "In design"],
  ["not_started", "Not started"],
  ["stopped", "Stopped"],
];

/** How many of a release's features are ready and where the rest are (valxel_sdlc T-99): on the
 * release's page and at its gate. */
export function Readiness({ r }: { r: ReleaseReadiness }) {
  return (
    <section className="readiness" aria-labelledby={`ready-${r.release_id}`}>
      <h3 id={`ready-${r.release_id}`}>{r.says}</h3>
      {r.must.missing.length > 0 && (
        <p className="small">
          <strong>Must-haves not ready:</strong> {r.must.missing.join(", ")}
        </p>
      )}
      {GROUPS.filter(([k]) => r.counts[k] > 0).map(([k, label]) => (
        <div key={k}>
          <h4 className="small">
            {label} ({r.counts[k]})
          </h4>
          <ul className="small">
            {k === "not_started"
              ? r.not_started.map((c) => (
                  <li key={c.title}>
                    {c.title}
                    {c.must && <span className="chip stop small">Must-have</span>}
                  </li>
                ))
              : r.features
                  .filter((f) => f.group === k)
                  .map((f) => (
                    <li key={f.feature_id}>
                      <Link to={threadPath(f.feature_id)}>{f.feature_id}</Link> {f.title}
                      {f.stage && k !== "ready" && k !== "stopped" ? (
                        <span className="faint"> ({f.stage})</span>
                      ) : null}
                      {f.must && <span className="chip stop small">Must-have</span>}
                    </li>
                  ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
