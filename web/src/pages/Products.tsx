import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Md } from "../components/Md";
import { api, ApiError, type InceptionReport, type InceptionSummary, type InceptionView, type Me, type Station } from "../api";
import { useLive } from "../live";
import { Rail } from "../components/Rail";
import { money, threadPath } from "../vocab";
import { Pending } from "./Thread";
import { FirstFeature, RecordPrs } from "../components/NextSteps";

const STATUS: Record<string, { text: string; chip: string }> = {
  researching: { text: "Researching the problem and the market", chip: "run" },
  drafting_brief: { text: "Agents writing the brief", chip: "run" },
  brief_gate: { text: "Waiting for the brief gate", chip: "wait" },
  drafting_plan: { text: "Agents writing the architecture and roadmap", chip: "run" },
  product_gate: { text: "Waiting for the product gate", chip: "wait" },
  set_up: { text: "Set up: pull requests open", chip: "go" },
  dropped: { text: "Stopped", chip: "stop" },
};

/** The inception as a line: two documents, two gates, then setup. */
function stations(status: string, running: boolean): Station[] {
  const order = [status === "researching" ? "researching" : "drafting_brief", "brief_gate", "drafting_plan", "product_gate", "set_up"];
  const at = order.indexOf(status);
  const keys: [string, "phase" | "gate"][] = [["brief", "phase"], ["brief", "gate"], ["product_plan", "phase"],
    ["product", "gate"], ["setup", "phase"]];
  return keys.map(([key, kind], i) => ({
    key, kind,
    state: status === "dropped" ? (i === 0 ? "done" : "todo")
      : i < at || status === "set_up" ? "done"
      : i === at ? (kind === "gate" ? "waiting" : running ? "current" : "current") : "todo",
  }));
}

function lines(text: string): string[] {
  return text.split("\n").map((l) => l.trim()).filter(Boolean);
}

function NewProduct({ me, onCancel }: { me: Me; onCancel: () => void }) {
  const nav = useNavigate();
  const [existing, setExisting] = useState("");
  const [name, setName] = useState("");
  const [org, setOrg] = useState("");
  const [vision, setVision] = useState("");
  const [goals, setGoals] = useState("");
  const [users, setUsers] = useState("");
  const [constraints, setConstraints] = useState("");
  const [budget, setBudget] = useState("");
  const [research, setResearch] = useState("quick");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const r = await api.createInception({
        name: existing ? (me.lanes.find((l) => l.id === existing)?.name ?? existing) : name, org: existing ? "" : org,
        vision, goals: lines(goals), users: lines(users), constraints: lines(constraints),
        budget_usd: Number(budget) || 0, product_id: existing, research,
      });
      nav(threadPath(r.feature_id));
    } catch (x) {
      setErr((x as Error).message);
      setBusy(false);
    }
  }
  return (
    <form className="panel form-panel" onSubmit={submit}>
      <h2 className="section-title">Start a product</h2>
      <p className="small muted">An agent researches the problem and the market, then agents write the product brief. The product council approves it before any
        architecture is written, then approves the architecture and roadmap together.</p>
      <label className="field">Product
        <select value={existing} onChange={(e) => setExisting(e.target.value)}>
          <option value="">A new product</option>
          {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}: describe it and plan what's next</option>)}
        </select>
      </label>
      {!existing && (
        <div className="split">
          <label className="field">Name<input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} /></label>
          <label className="field">GitHub org (it must already exist)
            <input value={org} onChange={(e) => setOrg(e.target.value)} required pattern="[A-Za-z0-9][A-Za-z0-9-]*" placeholder="acme-water" />
          </label>
        </div>
      )}
      <label className="field">The idea: the problem, for whom, and what changes for them
        <textarea value={vision} onChange={(e) => setVision(e.target.value)} required minLength={20} rows={5} />
      </label>
      <label className="field">Goals, one per line, each with its measure
        <textarea value={goals} onChange={(e) => setGoals(e.target.value)} rows={3}
          placeholder="Fewer dry-field incidents (measure: under 2 a month per farm)" />
      </label>
      <div className="split">
        <label className="field">Users, one per line
          <textarea value={users} onChange={(e) => setUsers(e.target.value)} rows={3} placeholder="Farm managers: out in the fields on a phone" />
        </label>
        <label className="field">Constraints, one per line (compliance, timeline, platform)
          <textarea value={constraints} onChange={(e) => setConstraints(e.target.value)} rows={3} />
        </label>
      </div>
      <label className="field" style={{ maxWidth: 260 }}>Budget for the first year of features (USD)
        <input type="number" min={0} step={100} value={budget} onChange={(e) => setBudget(e.target.value)} />
      </label>
      <fieldset className="field">
        <legend>Research before the plan</legend>
        <label className="small"><input type="radio" name="research" value="quick" checked={research === "quick"}
          onChange={() => setResearch("quick")} /> Quick: a few web searches for the alternatives and the market</label>
        <label className="small"><input type="radio" name="research" value="thorough" checked={research === "thorough"}
          onChange={() => setResearch("thorough")} /> Thorough: more alternatives, public pricing, market size</label>
        <label className="small"><input type="radio" name="research" value="own" checked={research === "own"}
          onChange={() => setResearch("own")} /> I've done my own: it's in what I wrote above</label>
        <span className="small faint">Every claim the plan rests on cites this research, with a source and a confidence.</span>
      </fieldset>
      <p className="small faint">Until the brief gate approves a data class, only models under a BAA read this.</p>
      {err && <p className="error-note" role="alert">{err}</p>}
      <div className="submit-row">
        <button className="btn" disabled={busy}>Write the brief</button>
        <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

export function ProductsPage({ me }: { me: Me }) {
  const [rows, setRows] = useState<InceptionSummary[] | null>(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState(false);
  const load = useCallback(() => { api.inceptions().then((r) => { setRows(r); setErr(""); }).catch((e) => setErr(e.message)); }, []);
  useEffect(load, [load]);
  useLive(load, (e) => e.type.startsWith("feature."));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Products</h1>
          <p className="muted">Start a product from a vision: a brief, then its architecture and roadmap, each approved by the product council.</p>
        </div>
        {!form && <button className="btn" onClick={() => setForm(true)}>Start a product</button>}
      </div>
      {form && <NewProduct me={me} onCancel={() => setForm(false)} />}
      {err && <p className="error-note" role="alert">{err}</p>}
      {rows && rows.length === 0 && !form && (
        <div className="panel empty"><h2>No products started here yet</h2>
          <p>Describe the product and its goals; agents write the brief for the product council.</p></div>
      )}
      {rows && rows.length > 0 && (
        <div className="rows">
          {rows.map((r) => (
            <Link key={r.id} to={threadPath(r.id)} className="row" style={{ gridTemplateColumns: "minmax(220px, 1.2fr) minmax(260px, 2fr) minmax(200px, 1fr) 72px" }}>
              <div><div className="row-title">{r.title}</div>
                <div className="row-id"><span className="mono">{r.id}</span>{r.existing ? ` describing ${r.product_id}` : ` in ${r.org}`}, from {r.created_by}</div></div>
              <Rail stations={stations(r.status, r.running)} compact />
              <div className="row-status"><span className={`chip ${STATUS[r.status]?.chip ?? "plain"}`}>{STATUS[r.status]?.text ?? r.status}</span></div>
              <div className="row-cost">{money(r.cost_usd)}</div>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}

function Report({ r }: { r?: InceptionReport }) {
  if (!r) return null;
  return (
    <div className="small" style={{ margin: "4px 0 12px" }}>
      <span className={`chip ${r.status === "passed" ? "go" : "stop"}`}>{r.status === "passed" ? "Checks passed" : "Checks didn't pass"}</span>{" "}
      <span className="muted">after {r.attempts} attempt{r.attempts === 1 ? "" : "s"}, {money(r.cost_usd)}, as {r.data_class === "phi" ? "PHI (strictest)" : r.data_class} data</span>
      {r.findings.length > 0 && <ul className="findings">{r.findings.map((f) => <li key={f}>{f}</li>)}</ul>}
    </div>
  );
}

export function InceptionPage() {
  const { id = "" } = useParams();
  const [v, setV] = useState<InceptionView | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<"research" | "brief" | "architecture" | "roadmap" | "setup">("brief");
  const load = useCallback(() => {
    api.inception(id).then((x) => { setV(x); setErr(""); })
      .catch((e) => setErr(e instanceof ApiError && e.status === 404 ? "This product inception doesn't exist, or it's still starting." : e.message));
  }, [id]);
  useEffect(load, [load]);
  useLive(load, (e) => e.feature_id === id);
  useEffect(() => {
    if (!v) return;
    if (v.status === "set_up") setTab("setup");
    else if (v.status === "product_gate") setTab("architecture");
  }, [v?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (err) return <p className="error-note" role="alert">{err}</p>;
  if (!v) return <p className="faint">Loading…</p>;
  const st = STATUS[v.status] ?? { text: v.status, chip: "plain" };
  const tabs = [["research", "Research"], ["brief", "Brief"], ["architecture", "Architecture"], ["roadmap", "Roadmap"], ["setup", "Setup"]] as const;
  const docs: Record<string, string> = { research: v.research ?? "", brief: v.brief, architecture: v.architecture, roadmap: v.roadmap };
  return (
    <>
      <div className="feature-head">
        <span className="small muted"><Link to="/products">Products</Link> / <span className="mono">{v.feature_id}</span></span>
        <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
          <h1>{v.title}</h1><span className={`chip ${st.chip}`}>{st.text}</span>
        </div>
        <div className="meta">
          <span>{v.vision.existing ? `Describing ${v.vision.product_id}` : `New product in ${v.vision.org}`}</span>
          <span>From {v.created_by}</span>
          <span>Decided by {v.council.join(", ")}</span>
          <span>{money(v.cost_usd)} spent</span>
        </div>
      </div>
      <section className="panel rail-panel" aria-label="Where the product is">
        <Rail stations={stations(v.status, v.running)} />
      </section>
      <Pending items={v.pending} id={v.feature_id} title={v.title} onDone={load} />
      <section className="panel" aria-label="Documents">
        <div className="tabs" role="tablist">
          {tabs.map(([k, label]) => (
            <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{label}</button>
          ))}
        </div>
        <div className="artifact-body" style={{ maxHeight: "none" }}>
          {tab !== "setup" && (
            docs[tab]
              ? <><Report r={v.reports[tab as keyof typeof v.reports]} /><div className="md"><Md>{docs[tab]}</Md></div></>
              : <p className="muted" style={{ paddingTop: 12 }}>{tab === "research" ? "An agent is researching the problem and the market." : tab === "brief" ? "Agents are writing the brief." : "Written after the brief gate approves the brief."}</p>
          )}
          {tab === "setup" && (
            v.setup.steps ? (
              <ul className="ext-list" style={{ padding: "12px 0" }}>
                {v.setup.steps.map((s) => (
                  <li key={s.step}>
                    <span><span className={`chip ${s.ok ? (s.skipped ? "wait" : "go") : "stop"}`}>{s.ok ? (s.skipped ? "To do by hand" : "Done") : "Failed"}</span>{" "}
                      <strong>{{ governance_repo: "Governance repo", governance_pr: "Governance pull request", registry_pr: "Registry pull request", teams: "Approver teams" }[s.step] ?? s.step}</strong></span>
                    {s.url && <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a>}
                    {s.repo && !s.url && <span className="mono">{s.repo}</span>}
                    {s.error && <span className="small error-note">{s.error}</span>}
                    {s.why && <span className="small muted">{s.why}</span>}
                    {s.line && s.skipped && <code className="small">{s.line}</code>}
                    {s.teams && <span className="small mono">{s.teams.join(", ")}</span>}
                  </li>
                ))}
                <li><RecordPrs id={v.feature_id} /></li>
                <li><FirstFeature id={v.feature_id} /></li>
                <li><span className="small muted">Next: merge the governance pull request, then the registry one. When the tool reloads its
                  config, the product's initiatives are on the Roadmap page, ready to start; each feature creates the repos the
                  architecture names when its design passes G4.</span></li>
              </ul>
            ) : <p className="muted" style={{ paddingTop: 12 }}>Runs when the product gate approves.</p>
          )}
        </div>
      </section>
    </>
  );
}
