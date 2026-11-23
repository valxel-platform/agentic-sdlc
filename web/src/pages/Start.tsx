import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type Billing, type Me, type Plan, type Plans, type ToolRow } from "../api";

/** The sign-up wizard (the sign-in design, A9): name the workspace, connect GitHub (both Apps, or a
 * guided account first), then describe the first product. A plan step comes first only when more
 * than one plan is offered (VALXEL_PLANS; by default only the free self-hosted plan is). A founder reaches the
 * product gate in about 10 minutes. Each step is safe to repeat: the wizard reads where things
 * stand from the server each time it opens, so coming back from GitHub lands on the next step. */
export function StartPage({ onChanged }: { me: Me; onChanged: () => void }) {
  const { ws } = useParams();
  return ws ? <Setup ws={ws} /> : <ChoosePlan onChanged={onChanged} />;
}

const STEPS = ["Plan", "Workspace", "GitHub", "First product"];

/** The steps, with `at` counted over all four; without a plan step, the first is dropped. */
function Steps({ at, withPlan = true }: { at: number; withPlan?: boolean }) {
  const steps = withPlan ? STEPS : STEPS.slice(1);
  const here = withPlan ? at : at - 1;
  return (
    <ol className="wizard-steps" aria-label="Sign-up steps">
      {steps.map((s, i) => (
        <li
          key={s}
          className={i < here ? "done" : i === here ? "current" : ""}
          aria-current={i === here ? "step" : undefined}
        >
          <span className="n" aria-hidden="true">
            {i < here ? "✓" : i + 1}
          </span>{" "}
          {s}
        </li>
      ))}
    </ol>
  );
}

function ChoosePlan({ onChanged }: { onChanged: () => void }) {
  const [plans, setPlans] = useState<Plans | null>(null);
  const [plan, setPlan] = useState<Plan["id"] | "">("");
  const [qualifies, setQualifies] = useState(false);
  const [form, setForm] = useState({ id: "", name: "" });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  useEffect(() => {
    api
      .plans()
      .then(setPlans)
      .catch((e) => setErr(e.message));
  }, []);

  // One plan offered (the default: self-hosted only): no plan step; this install is where the
  // workspace lives. With several, self-hosted means "install it yourself" and makes no workspace.
  const only = plans?.plans.length === 1 ? plans.plans[0].id : "";
  const chosen = only || plan;
  const makesWorkspace = !!chosen && (!!only || chosen !== "self-hosted");

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!makesWorkspace) return;
    setBusy(true);
    try {
      const w = await api.createWorkspace({ ...form, plan: chosen as Plan["id"] });
      onChanged();
      nav(`/start/${w.id}`);
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Set up Valxel</h1>
          <p className="muted">
            {only
              ? "About 10 minutes: a name, GitHub, then your first product."
              : "About 10 minutes: a plan, a name, GitHub, then your first product."}
          </p>
        </div>
      </div>
      <Steps at={makesWorkspace ? 1 : 0} withPlan={!only} />
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      {plans && !only && (
        <section className="panel" aria-labelledby="h-plan">
          <h2 id="h-plan" className="section-title">
            Choose a plan
          </h2>
          <p className="small muted pad">
            You can change between Lite and Full later. A card is asked for only when the plan first costs
            something.
          </p>
          <div className="plan-grid pad" role="radiogroup" aria-labelledby="h-plan">
            {plans?.plans.map((p) => (
              <label key={p.id} className={`plan-card ${plan === p.id ? "picked" : ""}`}>
                <input
                  type="radio"
                  name="plan"
                  value={p.id}
                  checked={plan === p.id}
                  onChange={() => setPlan(p.id)}
                />
                <strong>{p.name}</strong>
                <span className="small">{p.for_whom}</span>
                <span>{p.price}</span>
                <span className="small muted">
                  Card: {p.card_when}. Sign in with {p.sign_in}.
                </span>
                <span className="small muted">{p.runs_on}.</span>
              </label>
            ))}
          </div>
        </section>
      )}

      {!only && plan === "self-hosted" && plans && (
        <section className="panel" aria-labelledby="h-self">
          <h2 id="h-self" className="section-title">
            Self-hosted
          </h2>
          <div className="pad stack">
            <p>
              You run Valxel on your own infrastructure, so there's no hosted workspace to set up here. It's
              free under the license's grant, with no key to enter.
            </p>
            <label className="check">
              <input type="checkbox" checked={qualifies} onChange={(e) => setQualifies(e.target.checked)} />{" "}
              Our organization has under 250 people and under $10M in yearly revenue.
            </label>
            {qualifies ? (
              <p>
                <a className="btn" href={plans.install_guide} target="_blank" rel="noreferrer">
                  Open the install guide
                </a>
              </p>
            ) : (
              <p className="small muted">
                Larger organizations: choose Lite or Full, or talk to us about a license.
              </p>
            )}
          </div>
        </section>
      )}

      {makesWorkspace && (
        <section className="panel form-panel" aria-labelledby="h-ws">
          <h2 id="h-ws" className="section-title">
            Name your workspace
          </h2>
          <p className="small muted pad">
            A workspace holds your team's products, its people and who approves each gate.
            {chosen === "lite" && plans
              ? ` Hosted dev and staging are free for ${plans.trial_days} days.`
              : ""}
          </p>
          <form className="stack pad" onSubmit={create}>
            <label className="field">
              Name
              <input
                value={form.name}
                required
                maxLength={80}
                placeholder="Acme Farms"
                autoFocus
                onChange={(e) =>
                  setForm({
                    name: e.target.value,
                    id: e.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^-|-$/g, "")
                      .slice(0, 40),
                  })
                }
              />
            </label>
            <label className="field">
              Id <span className="small muted">lowercase letters, digits and dashes; can't change later</span>
              <input
                value={form.id}
                required
                pattern="[a-z0-9][a-z0-9\-]{2,39}"
                className="mono"
                onChange={(e) => setForm({ ...form, id: e.target.value })}
              />
            </label>
            <div className="submit-row">
              <button className="btn" type="submit" disabled={busy}>
                Create workspace
              </button>
            </div>
          </form>
        </section>
      )}
    </>
  );
}

function Setup({ ws }: { ws: string }) {
  const [rows, setRows] = useState<ToolRow[] | null>(null);
  const [billing, setBilling] = useState<Billing | null>(null);
  const [err, setErr] = useState("");
  const [noAccount, setNoAccount] = useState(false);
  const [skipped, setSkipped] = useState(() => readSkip(ws));
  const [q] = useSearchParams();
  const nav = useNavigate();
  useEffect(() => {
    api
      .connections(ws)
      .then(setRows)
      .catch((e) => setErr(e.message));
    api
      .billing(ws)
      .then(setBilling)
      .catch(() => {});
  }, [ws]);

  const row = (tool: string) => rows?.find((r) => r.tool === tool);
  const delivery = row("github")?.status === "connected";
  const provisioning = row("github-provisioning")?.status === "connected" || skipped;
  const at = !delivery || !provisioning ? 2 : 3;
  const back = encodeURIComponent(`/start/${ws}`);
  const install = (tool: string) =>
    `/api/workspaces/${encodeURIComponent(ws)}/connections/${tool}/install?back=${back}`;
  const message = q.get("error") ?? q.get("note") ?? "";

  async function firstProduct() {
    try {
      const s = await api.intakeStart(ws);
      nav(`/intake/${s.id}`);
    } catch (ex) {
      setErr((ex as Error).message);
    }
  }

  function skip() {
    try {
      sessionStorage.setItem(`valxel-skip-provisioning-${ws}`, "1");
    } catch {
      /* the choice lasts this page only */
    }
    setSkipped(true);
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Set up Valxel</h1>
          <p className="muted">
            Workspace <span className="mono">{ws}</span>
            {billing?.trial_days_left != null ? ` · hosted trial: ${billing.trial_days_left} days left` : ""}
          </p>
        </div>
      </div>
      <Steps at={at} withPlan={!!billing?.plan.hosted} />
      {(err || message) && (
        <p className={q.get("error") || err ? "error-note" : "note"} role="alert">
          {err || message}
        </p>
      )}
      {q.get("connected") && !q.get("error") && (
        <p className="note" aria-live="polite">
          Connected {q.get("connected")}.
        </p>
      )}

      {rows && !delivery && (
        <section className="panel" aria-labelledby="h-gh">
          <h2 id="h-gh" className="section-title">
            Connect GitHub
          </h2>
          <div className="pad stack">
            <p>
              Your products' code lives in your own GitHub organization. Valxel's GitHub App opens pull
              requests and reads checks and security alerts on the repos you choose. It has no admin rights.
            </p>
            {row("github")?.available === false ? (
              <p className="error-note">
                GitHub installs aren't set up on this Valxel yet. Whoever runs it sets the App's settings.
              </p>
            ) : !noAccount ? (
              <p className="row-gap">
                <a className="btn" href={install("github")}>
                  Install Valxel on GitHub
                </a>{" "}
                <button className="btn secondary" onClick={() => setNoAccount(true)}>
                  I don't have a GitHub organization yet
                </button>
              </p>
            ) : (
              <div className="stack">
                <p>
                  <strong>Make a GitHub account and organization</strong> (about 5 minutes, free):
                </p>
                <ol>
                  <li>
                    <a href="https://github.com/signup" target="_blank" rel="noreferrer">
                      Create a personal GitHub account
                    </a>
                    . Choose the free plan.
                  </li>
                  <li>
                    <a
                      href="https://github.com/account/organizations/new?plan=free"
                      target="_blank"
                      rel="noreferrer"
                    >
                      Create an organization
                    </a>{" "}
                    for the product, on the free plan. Your products' repos will live there, and you can add
                    your team later.
                  </li>
                  <li>Come back here and install Valxel on that organization.</li>
                </ol>
                <p>
                  <a className="btn" href={install("github")}>
                    I've made it: install Valxel
                  </a>
                </p>
              </div>
            )}
          </div>
        </section>
      )}

      {rows && delivery && !provisioning && (
        <section className="panel" aria-labelledby="h-prov">
          <h2 id="h-prov" className="section-title">
            Let Valxel create your repos
          </h2>
          <div className="pad stack">
            <p>
              A second App creates repos and sets their rules. It does that only after you approve a gate, and
              only Valxel's provisioning worker can use it.
            </p>
            <p className="row-gap">
              <a className="btn" href={install("github-provisioning")}>
                Install the repo-creation App
              </a>{" "}
              <button className="btn secondary" onClick={skip}>
                Skip: I'll create repos myself
              </button>
            </p>
            <p className="small muted">
              If you skip, Valxel lists the repos for you to create when a product needs them.
            </p>
          </div>
        </section>
      )}

      {rows && delivery && provisioning && (
        <section className="panel" aria-labelledby="h-first">
          <h2 id="h-first" className="section-title">
            Your first product
          </h2>
          <div className="pad stack">
            <p>
              Describe the idea in your own words. Agents research it and write the product brief, the
              architecture and a first roadmap. Then the product gate asks your workspace's Product group,
              which includes you, to approve them.
            </p>
            <p>
              <button className="btn" onClick={firstProduct}>
                Describe your first product
              </button>
            </p>
            <p className="small muted">
              You can also{" "}
              <Link to={`/workspaces/${encodeURIComponent(ws)}?tab=people`}>invite your team</Link> or{" "}
              <Link to={`/workspaces/${encodeURIComponent(ws)}?tab=connections`}>connect other tools</Link> at
              any point. AWS comes later, at go-live.
            </p>
            {billing?.note && <p className="small muted">{billing.note}</p>}
          </div>
        </section>
      )}
    </>
  );
}

function readSkip(ws: string): boolean {
  try {
    return sessionStorage.getItem(`valxel-skip-provisioning-${ws}`) === "1";
  } catch {
    return false;
  }
}
