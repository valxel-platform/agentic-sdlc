import type { IndependentReview } from "../api";

/** Each PR's independent review: an agent with none of the feature's context read it (T-144). */
export function IndependentReviews({ reviews }: { reviews: IndependentReview[] }) {
  if (!reviews.length) return null;
  return (
    <section className="mockups" aria-labelledby="independent-h">
      <h3 id="independent-h">Independent review</h3>
      <p className="small faint">
        A separate agent read each pull request with none of this feature&apos;s design or notes, so it checks
        what the change does rather than what the design said.
      </p>
      <ul className="plain">
        {reviews.map((r) => (
          <li key={`${r.repo}#${r.number}`}>
            <strong>
              {r.repo}#{r.number}: {r.state === "success" ? "approved" : "changes requested"}
            </strong>{" "}
            <span className="small muted">{r.description}</span>
            {r.summary && <p className="small">{r.summary}</p>}
            {r.findings.length > 0 && (
              <ul className="small">
                {r.findings.map((f, i) => (
                  <li key={i}>
                    <strong>{f.severity}</strong> <code>{f.file + (f.line ? `:${f.line}` : "")}</code>:{" "}
                    {f.issue}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
