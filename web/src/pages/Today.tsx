import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type FeatureSummary, type Me, type PendingItem, type ToolRow } from "../api";
import { useLive } from "../live";
import { ago, threadPath, waitingLabel } from "../vocab";
import { Stepper } from "../components/Stepper";
import banner from "../brand/valxel-banner.jpg";

const greeting = () => {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
};

/**
 * Today — composed client-side from existing calls only: inbox(true) for
 * decisions waiting on you, features() for your projects, connections() for
 * the connected-apps summary. No new endpoint.
 */
export function TodayPage({ me }: { me: Me }) {
  const [items, setItems] = useState<PendingItem[] | null>(null);
  const [features, setFeatures] = useState<FeatureSummary[] | null>(null);
  const [tools, setTools] = useState<ToolRow[] | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api
      .inbox(true)
      .then(setItems)
      .catch((e) => setErr(e.message));
    api
      .features()
      .then(setFeatures)
      .catch(() => {});
  }, []);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");
  useEffect(() => {
    const ws = Object.keys(me.roles ?? {})[0];
    if (ws)
      api
        .connections(ws)
        .then(setTools)
        .catch(() => {});
  }, [me.roles]);

  const waiting = items ?? [];
  const projects = (features ?? []).filter((f) => f.status !== "done" && f.status !== "aborted").slice(0, 5);
  const connected = (tools ?? []).filter((t) => t.status === "connected").length;
  const activity = [...(features ?? [])]
    .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))
    .slice(0, 4);

  return (
    <>
      <div className="today-hero">
        <img src={banner} alt="" />
        <div className="hero-text">
          <h1>{greeting()}. Here&rsquo;s what needs you.</h1>
          <p>
            {waiting.length
              ? `${waiting.length} decision${waiting.length === 1 ? "" : "s"} waiting`
              : "Nothing is waiting on you right now."}
          </p>
        </div>
      </div>
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      <div className="today-grid">
        <section className="panel" aria-labelledby="h-decisions">
          <h2 id="h-decisions" className="section-title">
            Decisions waiting
          </h2>
          <div className="pad stack">
            {waiting.length === 0 ? (
              <p className="muted">All caught up. Agents are working, or every feature has finished.</p>
            ) : (
              waiting.slice(0, 3).map((i) => (
                <div key={i.interrupt_id} className="today-card">
                  <h3>{i.title ?? i.feature_id}</h3>
                  <p className="small muted">{waitingLabel(i, i.product_id)}</p>
                  <p>
                    <Link to={i.feature_id ? threadPath(i.feature_id) : "/inbox"}>Decide</Link>
                  </p>
                </div>
              ))
            )}
            <p>
              <Link to="/inbox">All decisions</Link>
            </p>
          </div>
        </section>
        <section className="panel" aria-labelledby="h-projects">
          <h2 id="h-projects" className="section-title">
            Your projects
          </h2>
          <div className="pad">
            {projects.length === 0 ? (
              <p className="muted">
                No features in flight. <Link to="/intake">Start a request</Link>.
              </p>
            ) : (
              projects.map((f) => (
                <div key={f.feature_id} className="today-row">
                  <div className="grow">
                    <div className="row-title">
                      <Link to={`/features/${f.feature_id}`}>{f.title}</Link>
                    </div>
                    <div className="small faint">
                      <span className="mono">{f.feature_id}</span> · {ago(f.updated_at)}
                    </div>
                    <Stepper stations={f.rail} mini />
                  </div>
                </div>
              ))
            )}
            <p style={{ marginTop: 8 }}>
              <Link to="/board">All features</Link>
            </p>
          </div>
        </section>
        <section className="panel" aria-labelledby="h-apps">
          <h2 id="h-apps" className="section-title">
            Connected apps
          </h2>
          <div className="pad">
            {tools === null ? (
              <p className="muted">No workspace to check connections for yet.</p>
            ) : (
              <>
                <p className="small muted">
                  {connected} of {tools.length} apps connected
                </p>
                <ul className="plain-list small" style={{ marginTop: 8 }}>
                  {tools.map((t) => (
                    <li key={t.tool}>
                      {t.name}:{" "}
                      {t.status === "connected"
                        ? "Connected"
                        : t.status === "needs_attention"
                          ? "Needs attention"
                          : "Not connected"}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <p style={{ marginTop: 8 }}>
              <Link to="/connected-apps">Manage connections</Link>
            </p>
          </div>
        </section>
        <section className="panel" aria-labelledby="h-activity">
          <h2 id="h-activity" className="section-title">
            Latest activity
          </h2>
          <div className="pad">
            {activity.length === 0 ? (
              <p className="muted">Nothing yet.</p>
            ) : (
              <ul className="activity-list">
                {activity.map((f) => (
                  <li key={f.feature_id}>
                    <span>
                      <Link to={`/features/${f.feature_id}`}>{f.title}</Link>{" "}
                      <span className="small faint">· {ago(f.updated_at)}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <p style={{ marginTop: 8 }}>
              <Link to="/activity">Full history</Link>
            </p>
          </div>
        </section>
      </div>
    </>
  );
}
