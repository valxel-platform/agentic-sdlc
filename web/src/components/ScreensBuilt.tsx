import type { ScreensAtRelease } from "../api";

/** G6: each screen as built in staging (screenshot, accessibility scan) beside the mockup G3 approved. */
export function ScreensBuilt({ featureId, s }: { featureId: string; s: ScreensAtRelease }) {
  return (
    <section className="screens-built" aria-labelledby="built-h">
      <h3 id="built-h">The screens, as approved and as built</h3>
      {!s.scanned && <p className="small muted">No staging scan reported for these screens (phone screens and products
        without a scan aren't visited); compare them by hand.</p>}
      {s.screens.map((x) => (
        <div key={x.id} className="built-row">
          <div>
            <p className="small"><strong>{x.id}</strong> {x.name}{x.staging?.route ? <span className="mono faint"> {x.staging.route}</span> : null}</p>
            <div className="built-pair">
              <figure>
                <iframe title={`Approved mockup of ${x.id}`} sandbox="" src={`/api/features/${encodeURIComponent(featureId)}/mockups/${x.id}`} />
                <figcaption className="small faint">Approved at G3</figcaption>
              </figure>
              <figure>
                {x.staging?.desktop ? <img src={`/api/features/${encodeURIComponent(featureId)}/screens/${x.id}/desktop`} alt={`${x.id} ${x.name} in staging`} loading="lazy" />
                  : <div className="built-none small muted">No screenshot</div>}
                <figcaption className="small faint">Built, in staging{x.staging?.phone ? <> · <a href={`/api/features/${encodeURIComponent(featureId)}/screens/${x.id}/phone`} target="_blank" rel="noreferrer">phone</a></> : null}</figcaption>
              </figure>
            </div>
          </div>
          {x.violations.length > 0 ? (
            <ul className="findings small">{x.violations.map((v) => (
              <li key={v.fingerprint ?? v.title}><span className={`chip ${v.severity === "high" || v.severity === "critical" ? "stop" : "plain"}`}>
                {v.severity === "critical" ? "Critical" : v.severity === "high" ? "Serious" : "Minor"}</span> {v.rule}: {v.title.replace(`${x.id}: `, "")}</li>
            ))}</ul>
          ) : x.staging ? <p className="small"><span className="chip go">No accessibility problems found</span></p> : null}
        </div>
      ))}
    </section>
  );
}
