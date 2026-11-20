import { useState } from "react";
import type { MockupSet } from "../api";

/** G3: the UX spec's flow as a prototype to click through as a user (valxel_sdlc.prototype), or for
 * older features a mockup per screen; in a sandboxed frame (only the tool's own runtime runs, nothing
 * loaded from elsewhere, no access to the app), at desktop or phone width. A comment on a screen goes
 * back with "Request changes". */
export function Mockups({
  featureId,
  set,
  onComment,
}: {
  featureId: string;
  set: MockupSet;
  onComment?: (quote: string, text: string) => void;
}) {
  const [at, setAt] = useState(0);
  const [phone, setPhone] = useState<boolean | null>(null);
  const [text, setText] = useState("");
  const [visit, setVisit] = useState(0); // a new frame each time a screen is picked: start the walk there
  const [before, setBefore] = useState(false); // today's screen instead of the prototype
  if (!set.screens.length) {
    return set.findings.length ? <p className="small muted">{set.findings[0]}</p> : null;
  }
  const s = set.screens[Math.min(at, set.screens.length - 1)];
  const narrow = phone ?? s.phone;
  return (
    <section className="mockups" aria-labelledby="mock-h">
      <div className="doc-head">
        <h3 id="mock-h">{set.prototype ? "Try the flow" : "The screens"}</h3>
        <div className="segmented small" role="group" aria-label="Width">
          <button
            type="button"
            className={!narrow ? "on" : ""}
            aria-pressed={!narrow}
            onClick={() => setPhone(false)}
          >
            Desktop
          </button>
          <button
            type="button"
            className={narrow ? "on" : ""}
            aria-pressed={narrow}
            onClick={() => setPhone(true)}
          >
            Phone
          </button>
        </div>
      </div>
      <div className="mock-tabs" role="tablist">
        {set.screens.map((x, i) => (
          <button
            key={x.id}
            type="button"
            role="tab"
            aria-selected={i === at}
            onClick={() => {
              setAt(i);
              setVisit((v) => v + 1);
              setBefore(false);
            }}
          >
            <span className="mono">{x.id}</span> {x.name}
            {x.new ? (
              <span className="chip go small">New</span>
            ) : x.today ? (
              <span className="chip plain small">Changed</span>
            ) : null}
          </button>
        ))}
      </div>
      {set.prototype && s.today && (
        <div className="segmented small" role="group" aria-label={`${s.id}: today or with this change`}>
          <button
            type="button"
            className={before ? "on" : ""}
            aria-pressed={before}
            onClick={() => setBefore(true)}
          >
            Today
          </button>
          <button
            type="button"
            className={!before ? "on" : ""}
            aria-pressed={!before}
            onClick={() => setBefore(false)}
          >
            With this change
          </button>
        </div>
      )}
      <div className={`mock-frame${narrow ? " phone" : ""}`}>
        {set.prototype && before && s.today ? (
          <img
            className="today-shot"
            src={`/api/features/${encodeURIComponent(featureId)}/today/${s.id}/${
              (narrow && s.today.phone) || !s.today.desktop ? "phone" : "desktop"
            }`}
            alt={`${s.id} ${s.name} as it is today, in staging`}
          />
        ) : set.prototype ? (
          <iframe
            key={`${s.id}-${visit}`}
            title={`Prototype, starting at ${s.id}: ${s.name}`}
            sandbox="allow-scripts"
            src={`/api/features/${encodeURIComponent(featureId)}/prototype#${s.id}`}
          />
        ) : (
          <iframe
            title={`Mockup of ${s.id}: ${s.name}`}
            sandbox=""
            src={`/api/features/${encodeURIComponent(featureId)}/mockups/${s.id}`}
          />
        )}
      </div>
      {set.prototype ? (
        <p className="small faint">
          Click through it as a user would: fields keep what you type, the bar at the top switches each
          screen&apos;s states, and &ldquo;Show what&apos;s new&rdquo; outlines what this feature adds or
          changes. Pick a screen above to start there
          {set.screens.some((x) => x.today) ? ", and compare it with the screen as it is today" : ""}. Example
          values only. Not right? Comment on a screen and request changes: the design is drawn again.
        </p>
      ) : (
        <p className="small faint">
          A low-fidelity mockup from the UX spec{s.app ? `, for ${s.app}` : ""}: switch its states at the top.
          Example values only.
        </p>
      )}
      {set.findings.length > 0 && (
        <ul className="findings small">
          {set.findings.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
      {onComment && (
        <form
          className="line-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) {
              onComment(`${s.id} ${s.name}`, text.trim());
              setText("");
            }
          }}
        >
          <label className="visually-hidden" htmlFor="mock-c">
            Comment on this screen
          </label>
          <input
            id="mock-c"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={`Comment on ${s.id}: what should change on this screen?`}
          />
          <button className="btn secondary" disabled={!text.trim()}>
            Add comment
          </button>
        </form>
      )}
    </section>
  );
}
