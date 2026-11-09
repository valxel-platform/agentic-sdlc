import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Lesson, type LessonsOverview, type Me } from "../api";
import { useLive } from "../live";
import { PHASES, threadPath } from "../vocab";

const WHY: Record<string, string> = {
  remember: "Someone asked to remember it",
  takeover: "Someone took the phase over and rewrote it",
  repeat: "The same correction came up on several features",
};

function LessonRow({ l, proposed }: { l: Lesson; proposed: boolean }) {
  return (
    <li>
      <div>
        <strong>{l.id}</strong> <span className="chip plain">{PHASES[l.phase] ?? l.phase}</span>
        {l.check && <> <span className="chip go">On the checklist</span></>}
      </div>
      <p>{l.text}</p>
      <div className="small muted">
        {proposed && l.why ? `${WHY[l.why] ?? l.why}. ` : ""}
        {l.feature_id ? <>From <Link to={threadPath(l.feature_id)}>{l.feature_id}</Link>. </> : ""}
        {l.pr ? <a href={l.pr} target="_blank" rel="noreferrer">{proposed ? "Review the pull request" : "Pull request"}</a> : null}
      </div>
    </li>
  );
}

/** What the agents learned from people's corrections: lessons in the product's governance repo,
 * and proposals waiting for someone to review and merge. */
export function LessonsPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const productId = params.get("product") || me.lanes[0]?.id || "";
  const [data, setData] = useState<LessonsOverview | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    if (!productId) return;
    api.lessons(productId).then((r) => { setData(r); setErr(""); }).catch((e) => setErr(e.message));
  }, [productId]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Lessons</h1>
          <p className="muted">What agents learned from people's corrections. Merged lessons apply to features started after the merge.</p>
        </div>
        {me.lanes.length > 1 && (
          <div className="head-actions">
            <select aria-label="Product" value={productId} onChange={(e) => setParams({ product: e.target.value })}>
              {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          </div>
        )}
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {data && (
        <>
          <section className="panel" aria-labelledby="h-proposed">
            <h2 id="h-proposed" className="section-title">Waiting for review ({data.proposed.length})</h2>
            {data.proposed.length === 0
              ? <p className="pad muted">Nothing to review. When you send work back, tick "Remember this for future features", or take a phase over and edit it, and a lesson is proposed here.</p>
              : <ul className="ext-list">{data.proposed.map((l) => <LessonRow key={l.id} l={l} proposed />)}</ul>}
          </section>
          <section className="panel" aria-labelledby="h-approved">
            <h2 id="h-approved" className="section-title">In use ({data.approved.length})</h2>
            {data.approved.length === 0
              ? <p className="pad muted">No lessons yet. Merge a proposed lesson's pull request to put it in use.</p>
              : <ul className="ext-list">{data.approved.map((l) => <LessonRow key={l.id} l={l} proposed={false} />)}</ul>}
            <p className="small muted pad">{data.corrections} correction{data.corrections === 1 ? "" : "s"} recorded for this product so far.</p>
          </section>
        </>
      )}
    </>
  );
}
