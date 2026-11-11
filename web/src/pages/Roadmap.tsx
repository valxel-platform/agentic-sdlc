import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, type FeatureRow, type Me, type Roadmap, type RoadmapInitiative } from "../api";
import { useLive } from "../live";
import { GoalOutcomeLine, StatusChip, WantedForm } from "../components/Outcome";
import { HORIZONS, MODELS, money, threadPath } from "../vocab";

const STATUS: Record<string, string> = {
  drafting: "Drafting", awaiting_approval: "At G0", approved: "Approved", active: "Active", done: "Done", dropped: "Dropped",
  proposed: "Proposed", planned: "Planned", ready: "Ready", shipped: "Shipped", cancelled: "Cancelled",
};

function Progress({ i }: { i: RoadmapInitiative }) {
  const pct = i.budget_usd ? Math.min(100, (i.spent_usd / i.budget_usd) * 100) : 0;
  const started = i.progress.total;
  return (
    <>
      <p className="small">
        {i.progress.released} of {started} started feature{started === 1 ? "" : "s"} live
        {i.progress.planned > started && `, ${i.progress.planned - started} not started yet`}
      </p>
      <div className="bar" role="img" aria-label={`${money(i.spent_usd)} spent of ${money(i.budget_usd)}`}>
        <span style={{ width: `${pct}%` }} className={i.spent_usd > i.budget_usd ? "over" : ""} />
      </div>
      <p className="small muted">{money(i.spent_usd)} of {money(i.budget_usd)}</p>
    </>
  );
}

function InitiativeCard({ i }: { i: RoadmapInitiative }) {
  return (
    <li className="ini-card">
      <div className="work-line">
        <Link to={threadPath(i.id)} className="ini-title">{i.title}</Link>
        {i.status !== "approved" && <span className="chip plain">{STATUS[i.status] ?? i.status}</span>}
      </div>
      <p className="small muted">{i.id}{i.goal ? `, ${i.goal}` : ""}</p>
      {i.outcome && ["approved", "active"].includes(i.status) && (
        <p className="small"><StatusChip status={i.outcome.status} why={i.outcome.why} /> <span className="muted">goal since approval</span></p>
      )}
      {["approved", "active"].includes(i.status) && (i.review_by || (i.reviews ?? []).length > 0) && (
        <p className="small muted">
          {(i.reviews ?? []).filter((r) => r.status === "waiting").map((r) => (
            <span key={r.id}><Link to={threadPath(r.id)}>Review waiting</Link>: {r.why}. </span>
          ))}
          {i.review_by && !(i.reviews ?? []).some((r) => r.status === "waiting") && `Next review ${i.review_by}`}
        </p>
      )}
      {["approved", "active", "done"].includes(i.status) && <Progress i={i} />}
      <p className="small faint">
        {MODELS[i.release_model] ?? i.release_model}
        {i.record === "pull request" && ", record waiting for its pull request to merge"}
      </p>
    </li>
  );
}

function NewInitiative({ productId, onCancel }: { productId: string; onCancel: () => void }) {
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  const [request, setRequest] = useState("");
  const [measure, setMeasure] = useState("");
  const [horizon, setHorizon] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const r = await api.createInitiative({ product_id: productId, title, request, measure, horizon });
      nav(threadPath(r.feature_id));
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  }
  return (
    <form className="panel form-panel" onSubmit={submit}>
      <h2 className="section-title">New initiative</h2>
      <p className="small muted">An agent drafts the goal, measure, budget and the features that deliver it; product owners approve it at G0.</p>
      <label className="field">Title<input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={3} /></label>
      <label className="field">What outcome do you want, and what does it involve?
        <textarea value={request} onChange={(e) => setRequest(e.target.value)} required minLength={10}
          placeholder="Separate the parts with semicolons; each can become a feature." />
      </label>
      <label className="field">How will you measure it? (optional)<input value={measure} onChange={(e) => setMeasure(e.target.value)} /></label>
      <label className="field">When
        <select value={horizon} onChange={(e) => setHorizon(e.target.value)}>
          <option value="">Let the agent suggest</option>
          {Object.entries(HORIZONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {err && <p className="error-note" role="alert">{err}</p>}
      <div className="actions">
        <button className="btn" disabled={busy}>Draft initiative</button>
        <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function NewRelease({ productId, features, onCancel }: { productId: string; features: FeatureRow[]; onCancel: () => void }) {
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  const [model, setModel] = useState("launch");
  const [date, setDate] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const r = await api.createRelease({ product_id: productId, title, model, features: picked, target_date: date });
      nav(threadPath(r.feature_id));
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  }
  return (
    <form className="panel form-panel" onSubmit={submit}>
      <h2 className="section-title">New release</h2>
      <label className="field">Title<input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={3} /></label>
      <fieldset className="field">
        <legend>Kind</legend>
        <label className="radio"><input type="radio" name="model" checked={model === "launch"} onChange={() => setModel("launch")} />
          Launch group: each feature ships to production with its flag off; one approval turns them on together</label>
        <label className="radio"><input type="radio" name="model" checked={model === "train"} onChange={() => setModel("train")} />
          Release train: features wait after staging; one approval ships them together (regulated or contractual releases)</label>
      </fieldset>
      <label className="field">Target date (optional)<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
      <fieldset className="field">
        <legend>Features (in progress, before G6)</legend>
        {features.length === 0 && <p className="small muted">No features in progress.</p>}
        {features.map((f) => (
          <label key={f.feature_id} className="radio">
            <input type="checkbox" checked={picked.includes(f.feature_id)}
              onChange={(e) => setPicked((p) => e.target.checked ? [...p, f.feature_id] : p.filter((x) => x !== f.feature_id))} />
            {f.feature_id} {f.title} <span className="faint small">({f.stage || "starting"})</span>
          </label>
        ))}
      </fieldset>
      {err && <p className="error-note" role="alert">{err}</p>}
      <div className="actions">
        <button className="btn" disabled={busy || picked.length === 0}>Plan release</button>
        <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export function RoadmapPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const productId = params.get("product") || me.lanes[0]?.id || "";
  const [rm, setRm] = useState<Roadmap | null>(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState<null | "initiative" | "release">(null);

  const load = useCallback(() => {
    if (!productId) return;
    api.roadmap(productId).then((r) => { setRm(r); setErr(""); }).catch((e) => setErr(e.message));
  }, [productId]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated" || e.type === "feature.created");

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;
  const live = (rm?.initiatives ?? []).filter((i) => !["dropped", "done"].includes(i.status));
  const inFlight = live.filter((i) => !["approved", "active"].includes(i.status));
  const committed = live.filter((i) => ["approved", "active"].includes(i.status));
  const inProgress = [...committed.flatMap((i) => i.features), ...(rm?.unplanned ?? [])].filter((f) => f.status === "in_progress");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Roadmap</h1>
          <p className="muted">Goals, the initiatives that serve them, and how features ship together.</p>
        </div>
        <div className="head-actions">
          {me.lanes.length > 1 && (
            <select aria-label="Product" value={productId} onChange={(e) => setParams({ product: e.target.value })}>
              {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          )}
          <button className="btn" onClick={() => setForm("initiative")}>New initiative</button>
          <button className="btn secondary" onClick={() => setForm("release")}>New release</button>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {form === "initiative" && <NewInitiative productId={productId} onCancel={() => setForm(null)} />}
      {form === "release" && <NewRelease productId={productId} features={inProgress} onCancel={() => setForm(null)} />}
      {rm && (
        <div className="roadmap">
          <section className="panel" aria-labelledby="h-goals">
            <h2 id="h-goals" className="section-title">Goals</h2>
            {rm.goals.length === 0 ? (
              <p className="small muted pad">No goals yet. An initiative can propose one; approving it adds the goal to the roadmap.</p>
            ) : (
              <ul className="ext-list">
                {rm.goals.map((g) => (
                  <li key={g.id}><strong>{g.title}</strong> <span className="small muted">{g.id}{g.period ? `, ${g.period}` : ""}</span>
                    {g.measure && <div className="small">Measured by: {g.measure}</div>}
                    {g.outcome ? <GoalOutcomeLine o={g.outcome} /> :
                      <div className="small faint">Not measured: add a metric to this goal in roadmap/goals.yaml</div>}</li>
                ))}
              </ul>
            )}
          </section>

          {(rm.outcomes?.wanted.length ?? 0) > 0 && (
            <section className="panel" aria-labelledby="h-wanted">
              <h2 id="h-wanted" className="section-title">Numbers to enter</h2>
              <p className="small muted pad">These goals are entered by hand, and a feature's readout is due. Product owners enter the number for each period.</p>
              <ul className="ext-list">
                {rm.outcomes!.wanted.map((w) => (
                  <WantedForm key={`${w.goal_id}-${w.feature_id}-${w.window_days}`} productId={productId} w={w} onDone={load} />
                ))}
              </ul>
            </section>
          )}

          {inFlight.length > 0 && (
            <section className="panel" aria-labelledby="h-flight">
              <h2 id="h-flight" className="section-title">Being drafted or decided</h2>
              <ul className="ini-list">{inFlight.map((i) => <InitiativeCard key={i.id} i={i} />)}</ul>
            </section>
          )}

          <div className="horizons">
            {Object.entries(HORIZONS).map(([key, name]) => {
              const list = committed.filter((i) => i.horizon === key);
              return (
                <section key={key} className="panel" aria-labelledby={`h-${key}`}>
                  <h2 id={`h-${key}`} className="section-title">{name}</h2>
                  {list.length === 0 ? <p className="small faint pad">No initiatives.</p> :
                    <ul className="ini-list">{list.map((i) => <InitiativeCard key={i.id} i={i} />)}</ul>}
                </section>
              );
            })}
          </div>

          <section className="panel" aria-labelledby="h-rel">
            <h2 id="h-rel" className="section-title">Releases</h2>
            {rm.releases.length === 0 ? <p className="small muted pad">No releases planned. Features released on their own go live at G6.</p> : (
              <ul className="ext-list">
                {rm.releases.map((r) => (
                  <li key={r.id}>
                    <div className="work-line">
                      <Link to={threadPath(r.id)}>{r.title}</Link>
                      <span className="chip plain">{MODELS[r.model] ?? r.model}</span>
                      <span className={`chip ${r.status === "shipped" || r.status === "approved" ? "go" : r.status === "cancelled" ? "stop" : "wait"}`}>{STATUS[r.status] ?? r.status}</span>
                    </div>
                    <div className="small muted">{r.id}: {r.features.join(", ")}{r.target_date ? `, target ${r.target_date}` : ""}</div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {rm.unplanned.length > 0 && (
            <section className="panel" aria-labelledby="h-unplanned">
              <h2 id="h-unplanned" className="section-title">Unplanned work</h2>
              <p className="small muted pad">Features in progress that aren't part of an initiative.</p>
              <ul className="ext-list">
                {rm.unplanned.map((f) => (
                  <li key={f.feature_id}>
                    <div className="work-line"><Link to={threadPath(f.feature_id)}>{f.title}</Link><span className="chip plain">{f.stage || "Starting"}</span></div>
                    <div className="small muted">{f.feature_id}</div>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <p className="small faint">
            {rm.record.error ? `Couldn't read the roadmap record: ${rm.record.error}` : `Roadmap record: ${rm.record.repo}@${rm.record.sha.slice(0, 7)}, roadmap/`}
          </p>
        </div>
      )}
    </>
  );
}
