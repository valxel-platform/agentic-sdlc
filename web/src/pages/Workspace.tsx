import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  api,
  ROLE_TEXT,
  ROLES,
  type AuditEntry,
  type Group,
  type Invited,
  type Me,
  type Member,
  type Role,
  type ToolRow,
  type Workspace,
  type WorkspaceView,
} from "../api";
import { GATES } from "../vocab";
import { GcpConnectForm } from "../components/GcpConnectForm";

// The gates a workspace's groups can approve, in pipeline order (valxel_sdlc.workspaces.GATES).
const GATE_IDS = ["g0", "g1", "g2", "g3", "g4", "g5", "g6", "security", "oncall"];
const EXTRA_GATES: Record<string, string> = {
  security: "Security risk acceptance",
  oncall: "On-call actions",
};
const gateName = (g: string) => GATES[g]?.name ?? EXTRA_GATES[g] ?? g;
const isAdmin = (r?: Role) => r === "owner" || r === "admin";
const who = (m: Member) => [m.email, m.github_login && `@${m.github_login}`].filter(Boolean).join(" · ");

/** The workspaces you belong to, and a new one (you become its owner). */
export function WorkspacesPage() {
  const [list, setList] = useState<Workspace[] | null>(null);
  const [err, setErr] = useState("");
  const [form, setForm] = useState({ id: "", name: "", plan: "lite" });
  const nav = useNavigate();
  useEffect(() => {
    api
      .workspaces()
      .then(setList)
      .catch((e) => setErr(e.message));
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    try {
      const w = await api.createWorkspace(form);
      nav(`/workspaces/${w.id}`);
    } catch (ex) {
      setErr((ex as Error).message);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Workspaces</h1>
          <p className="muted">A workspace holds a team's products, its people and who approves each gate.</p>
        </div>
      </div>
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      {list && list.length > 0 && (
        <section className="panel" aria-labelledby="h-mine">
          <h2 id="h-mine" className="section-title">
            Yours
          </h2>
          <ul className="ext-list">
            {list.map((w) => (
              <li key={w.id}>
                <Link to={`/workspaces/${w.id}`}>
                  <strong>{w.name}</strong>
                </Link>{" "}
                <span className="chip plain">{w.role}</span> <span className="small muted mono">{w.id}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="panel form-panel" aria-labelledby="h-new">
        <h2 id="h-new" className="section-title">
          New workspace
        </h2>
        <form className="stack pad" onSubmit={create}>
          <label className="field">
            Name
            <input
              value={form.name}
              required
              maxLength={80}
              placeholder="Acme Farms"
              onChange={(e) =>
                setForm({
                  ...form,
                  name: e.target.value,
                  id:
                    form.id ||
                    e.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^-|-$/g, ""),
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
          <label className="field">
            Plan
            <select value={form.plan} onChange={(e) => setForm({ ...form, plan: e.target.value })}>
              <option value="lite">Lite</option>
              <option value="full">Full</option>
              <option value="self-hosted">Self-hosted</option>
            </select>
          </label>
          <div className="submit-row">
            <button className="btn" type="submit">
              Create workspace
            </button>
          </div>
        </form>
      </section>
    </>
  );
}

type Tab = "people" | "groups" | "gates" | "connections" | "audit";

/** One workspace: its people and roles, approver groups, who approves each product's gates, and
 * (for admins) the audit log. Everything here is checked again by the server. */
export function WorkspacePage({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const { id = "" } = useParams();
  const [w, setW] = useState<WorkspaceView | null>(null);
  const [params] = useSearchParams();
  const [tab, setTab] = useState<Tab>((params.get("tab") as Tab) || "people");
  const [err, setErr] = useState("");
  const load = useCallback(() => {
    api
      .workspace(id)
      .then((v) => {
        setW(v);
        setErr("");
      })
      .catch((e) => setErr(e.message));
  }, [id]);
  useEffect(load, [load]);
  const admin = isAdmin(w?.role);
  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setErr("");
      load();
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  if (!w)
    return err ? (
      <p className="error-note" role="alert">
        {err}
      </p>
    ) : null;
  const tabs: [Tab, string][] = [
    ["people", "People"],
    ["groups", "Approver groups"],
    ["gates", "Gate approvers"],
    ["connections", "Connections"],
    ...(admin ? [["audit", "Audit log"] as [Tab, string]] : []),
  ];
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{w.name}</h1>
          <p className="muted">
            You're {/^[aeiou]/.test(w.role) ? "an" : "a"} {w.role} here: {ROLE_TEXT[w.role].toLowerCase()}.
            {me.login && (
              <>
                {" "}
                Signed in as <span className="mono">{me.login}</span>.
              </>
            )}
          </p>
          {me.idp === "cognito" && (
            <p className="small">
              {me.strong ? (
                "You signed in with a passkey."
              ) : admin || w.role === "approver" ? (
                <>
                  To change this workspace or approve through a group,{" "}
                  <a href="/api/auth/login?provider=passkey&next=/workspaces">sign in with a passkey</a>.
                </>
              ) : null}{" "}
              <a href="/api/auth/passkey">Add a passkey</a>
            </p>
          )}
        </div>
      </div>
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      <div className="tabs" role="tablist">
        {tabs.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>
      <div role="tabpanel">
        {tab === "people" && <People w={w} me={me} admin={admin} run={run} />}
        {tab === "groups" && <Groups w={w} admin={admin} run={run} />}
        {tab === "gates" && <Gates w={w} admin={admin} run={run} />}
        {tab === "connections" && <Connections ws={w.id} admin={admin} />}
        {tab === "audit" && admin && <Audit ws={w.id} />}
      </div>
    </>
  );
}

type Run = (fn: () => Promise<unknown>) => Promise<void>;

function People({ w, me, admin, run }: { w: WorkspaceView; me: Me; admin: boolean; run: Run }) {
  const blank = { name: "", email: "", github_login: "", role: "member" as Role, products: [] as string[] };
  const [form, setForm] = useState(blank);
  const [sent, setSent] = useState<{ name: string; url: string; emailed: boolean } | null>(null);
  const owner = w.role === "owner";

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    await run(async () => {
      const m: Invited = await api.invite(w.id, form);
      setSent({ name: m.name, url: m.invite_url, emailed: m.emailed });
      setForm(blank);
    });
  }
  const resend = (m: Member) =>
    run(async () => {
      const r = await api.resendInvite(w.id, m.id);
      setSent({ name: m.name, url: r.invite_url, emailed: r.emailed });
    });

  return (
    <>
      <section className="panel" aria-labelledby="h-people">
        <h2 id="h-people" className="section-title">
          People ({w.members.length})
        </h2>
        <div className="table-scroll">
          <table className="quality">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Role</th>
                <th scope="col">Products</th>
                <th scope="col">Status</th>
                {admin && (
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {[...w.members]
                .sort((a, b) => ROLES.indexOf(a.role) - ROLES.indexOf(b.role) || a.name.localeCompare(b.name))
                .map((m) => {
                  // Only an owner changes an owner; nobody demotes the last owner (the server says why).
                  const editable = admin && (m.role !== "owner" || owner);
                  const self =
                    (!!m.github_login && m.github_login.toLowerCase() === me.login.toLowerCase()) ||
                    (!!m.email && m.email === (me.email ?? "").toLowerCase());
                  return (
                    <tr key={m.id}>
                      <td>
                        <strong>{m.name}</strong>
                        <div className="small muted">{who(m)}</div>
                      </td>
                      <td>
                        {editable ? (
                          <select
                            aria-label={`${m.name}'s role`}
                            value={m.role}
                            onChange={(e) =>
                              run(() => api.updateMember(w.id, m.id, { role: e.target.value as Role }))
                            }
                          >
                            {ROLES.filter((r) => r !== "owner" || owner).map((r) => (
                              <option key={r} value={r}>
                                {r}
                              </option>
                            ))}
                          </select>
                        ) : (
                          m.role
                        )}
                      </td>
                      <td className="small">{m.products.length ? m.products.join(", ") : "All"}</td>
                      <td>
                        <span className={`chip ${m.status === "active" ? "go" : "plain"}`}>{m.status}</span>
                      </td>
                      {admin && (
                        <td style={{ whiteSpace: "nowrap" }}>
                          {editable && m.status === "invited" && (
                            <button className="small-btn" onClick={() => resend(m)}>
                              New link
                            </button>
                          )}
                          {editable && !self && m.status !== "invited" && (
                            <button
                              className="small-btn"
                              onClick={() =>
                                run(() =>
                                  api.updateMember(w.id, m.id, {
                                    status: m.status === "suspended" ? "active" : "suspended",
                                  }),
                                )
                              }
                            >
                              {m.status === "suspended" ? "Reactivate" : "Suspend"}
                            </button>
                          )}
                          {editable && !self && m.status === "active" && (
                            <button
                              className="small-btn"
                              title="Ends their sessions on every device; they can sign in again"
                              onClick={() => run(() => api.signMemberOut(w.id, m.id))}
                            >
                              Sign out
                            </button>
                          )}
                          {editable && !self && (
                            <button
                              className="small-btn"
                              onClick={() => {
                                if (
                                  window.confirm(
                                    `Remove ${m.name} from ${w.name}? They lose access on their next click.`,
                                  )
                                )
                                  run(() => api.removeMember(w.id, m.id));
                              }}
                            >
                              Remove
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      </section>
      {sent && (
        <section className="panel note" aria-live="polite">
          <p className="pad">
            {sent.emailed
              ? `We emailed ${sent.name} an invitation.`
              : `Send ${sent.name} this link; it works once, for them alone.`}{" "}
            <span className="mono small">{sent.url}</span>{" "}
            <button className="small-btn" onClick={() => navigator.clipboard?.writeText(sent.url)}>
              Copy link
            </button>
          </p>
        </section>
      )}
      {admin && (
        <section className="panel form-panel" aria-labelledby="h-invite">
          <h2 id="h-invite" className="section-title">
            Add someone
          </h2>
          <form className="stack pad" onSubmit={invite}>
            <div className="grid-2">
              <label className="field">
                Name
                <input
                  value={form.name}
                  maxLength={80}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </label>
              <label className="field">
                Email
                <input
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                />
              </label>
              <label className="field">
                GitHub login
                <input
                  value={form.github_login}
                  className="mono"
                  placeholder="optional"
                  onChange={(e) => setForm({ ...form, github_login: e.target.value })}
                />
              </label>
              <label className="field">
                Role
                <select
                  value={form.role}
                  onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
                >
                  {ROLES.filter((r) => r !== "owner").map((r) => (
                    <option key={r} value={r}>
                      {r}: {ROLE_TEXT[r]}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {w.products.length > 1 && (
              <fieldset className="field">
                <legend>
                  Products <span className="small muted">none ticked: all of them</span>
                </legend>
                {w.products.map((p) => (
                  <label key={p.id} className="check">
                    <input
                      type="checkbox"
                      checked={form.products.includes(p.id)}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          products: e.target.checked
                            ? [...form.products, p.id]
                            : form.products.filter((x) => x !== p.id),
                        })
                      }
                    />{" "}
                    {p.id}
                  </label>
                ))}
              </fieldset>
            )}
            <div className="submit-row">
              <button
                className="btn"
                type="submit"
                disabled={!form.email.trim() && !form.github_login.trim()}
              >
                Invite
              </button>
              <span className="small muted">They sign in with that email or GitHub account to accept.</span>
            </div>
          </form>
        </section>
      )}
    </>
  );
}

function Groups({ w, admin, run }: { w: WorkspaceView; admin: boolean; run: Run }) {
  const [name, setName] = useState("");
  const people = w.members.filter((m) => m.status !== "suspended");
  const used = new Set(w.products.flatMap((p) => Object.values(p.gates).flat()));
  const toggle = (g: Group, id: string, on: boolean) =>
    run(() => api.setGroup(w.id, g.slug, on ? [...g.members, id] : g.members.filter((x) => x !== id)));

  return (
    <>
      {w.groups.map((g) => (
        <section key={g.slug} className="panel" aria-labelledby={`h-g-${g.slug}`}>
          <div className="panel-head">
            <h2 id={`h-g-${g.slug}`} className="section-title">
              {g.name} <span className="small muted">({g.members.length})</span>
            </h2>
            {admin && !used.has(g.slug) && (
              <button
                className="small-btn"
                onClick={() => {
                  if (window.confirm(`Delete the ${g.name} group?`)) run(() => api.deleteGroup(w.id, g.slug));
                }}
              >
                Delete
              </button>
            )}
          </div>
          {admin ? (
            <div className="pad team-list">
              {people.map((m) => (
                <label key={m.id} className="check">
                  <input
                    type="checkbox"
                    checked={g.members.includes(m.id)}
                    onChange={(e) => toggle(g, m.id, e.target.checked)}
                  />{" "}
                  {m.name}
                </label>
              ))}
            </div>
          ) : (
            <p className="pad">
              {g.members.map((id) => w.members.find((m) => m.id === id)?.name ?? id).join(", ") || (
                <span className="muted">Nobody yet</span>
              )}
            </p>
          )}
          {used.has(g.slug) && (
            <p className="small muted pad">
              Approves gates, so it can't be deleted: change the gate approvers first.
            </p>
          )}
        </section>
      ))}
      {admin && (
        <form
          className="panel form-panel stack pad"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => api.createGroup(w.id, name)).then(() => setName(""));
          }}
        >
          <label className="field">
            New group
            <input
              value={name}
              required
              maxLength={40}
              placeholder="QA"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <div className="submit-row">
            <button className="btn" type="submit">
              Add group
            </button>
          </div>
        </form>
      )}
    </>
  );
}

function Gates({ w, admin, run }: { w: WorkspaceView; admin: boolean; run: Run }) {
  if (!w.products.length) return <p className="muted pad">No products in this workspace yet.</p>;
  return (
    <>
      {w.products.map((p) => (
        <ProductGates key={p.id} w={w} product={p} admin={admin} run={run} />
      ))}
    </>
  );
}

function ProductGates({
  w,
  product,
  admin,
  run,
}: {
  w: WorkspaceView;
  product: WorkspaceView["products"][number];
  admin: boolean;
  run: Run;
}) {
  const [gates, setGates] = useState<Record<string, string[]>>(product.gates);
  useEffect(() => setGates(product.gates), [product.gates]);
  const changed = JSON.stringify(gates) !== JSON.stringify(product.gates);
  const flip = (gate: string, slug: string, on: boolean) =>
    setGates((g) => {
      const next = on ? [...(g[gate] ?? []), slug] : (g[gate] ?? []).filter((x) => x !== slug);
      const out = { ...g, [gate]: next };
      if (!next.length) delete out[gate];
      return out;
    });
  return (
    <section className="panel" aria-labelledby={`h-p-${product.id}`}>
      <div className="panel-head">
        <h2 id={`h-p-${product.id}`} className="section-title">
          {product.id}
          <span className="small muted"> · version {product.version || "none yet"}</span>
        </h2>
        {admin && (
          <button
            className="btn"
            disabled={!changed}
            onClick={() => run(() => api.setGates(w.id, product.id, gates))}
          >
            Save as a new version
          </button>
        )}
      </div>
      <p className="small muted pad">
        These groups approve alongside the teams in the product's governance repo. Features already under way
        keep the version they started with.
      </p>
      <div className="table-scroll">
        <table className="quality">
          <thead>
            <tr>
              <th scope="col">Gate</th>
              {w.groups.map((g) => (
                <th key={g.slug} scope="col">
                  {g.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {GATE_IDS.map((gate) => (
              <tr key={gate}>
                <th scope="row">{gateName(gate)}</th>
                {w.groups.map((g) => {
                  const on = (gates[gate] ?? []).includes(g.slug);
                  return (
                    <td key={g.slug}>
                      {admin ? (
                        <input
                          type="checkbox"
                          aria-label={`${g.name} approves ${gateName(gate)}`}
                          checked={on}
                          onChange={(e) => flip(gate, g.slug, e.target.checked)}
                        />
                      ) : on ? (
                        "✓"
                      ) : (
                        ""
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const STATUS: Record<ToolRow["status"], [string, string]> = {
  connected: ["Connected", "go"],
  needs_attention: ["Needs attention", "stop"],
  not_connected: ["Not connected", "plain"],
};
const when = (t?: number) => (t ? new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ") : "");

/** Every tool, connected or not: what Valxel can reach, and the one way to fix what's broken. */
function Connections({ ws, admin }: { ws: string; admin: boolean }) {
  const [rows, setRows] = useState<ToolRow[] | null>(null);
  const [params] = useSearchParams();
  const [err, setErr] = useState(params.get("error") ?? "");
  const [note, setNote] = useState(
    params.get("connected") ? `Connected ${params.get("connected")}.` : (params.get("note") ?? ""),
  );
  const load = useCallback(() => {
    api
      .connections(ws)
      .then(setRows)
      .catch((e) => setErr(e.message));
  }, [ws]);
  useEffect(load, [load]);
  const install = (tool: string) => {
    // eslint-disable-next-line react-hooks/immutability -- install navigation, not a render mutation.
    window.location.href = `/api/workspaces/${encodeURIComponent(ws)}/connections/${tool}/install`;
  };
  async function disconnect(tool: string, account: string, name: string) {
    if (
      !window.confirm(
        `Disconnect ${name} for ${account}? Features that need it will wait until it's connected again.`,
      )
    )
      return;
    try {
      const r = await api.disconnect(ws, tool, account);
      setNote(`Disconnected. To remove Valxel's access on ${name}'s side too: ${r.revoke}.`);
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  if (!rows)
    return err ? (
      <p className="error-note" role="alert">
        {err}
      </p>
    ) : null;
  return (
    <>
      {err && (
        <p className="error-note" role="alert">
          {err}
        </p>
      )}
      {note && (
        <p className="note" aria-live="polite">
          {note}
        </p>
      )}
      {rows.map((t) => {
        const [label, chip] = STATUS[t.status];
        return (
          <section key={t.tool} className="panel" aria-labelledby={`h-c-${t.tool}`}>
            <div className="panel-head">
              <h2 id={`h-c-${t.tool}`} className="section-title">
                {t.name} <span className={`chip ${chip}`}>{label}</span>
              </h2>
              {admin && t.available && t.tool !== "aws" && t.tool !== "gcp" && (
                <button
                  className={t.status === "connected" ? "btn secondary" : "btn"}
                  onClick={() => install(t.tool)}
                >
                  {t.status === "not_connected"
                    ? "Connect"
                    : t.status === "needs_attention"
                      ? "Reconnect"
                      : "Add an account"}
                </button>
              )}
            </div>
            <p className="small muted pad">
              {t.grants}. Asked for at: {t.asked_when}.
              {!t.available &&
                " It isn't set up on this Valxel yet: whoever runs Valxel adds its OAuth client."}
            </p>
            {t.connections.map((c) => (
              <div key={c.account} className="pad">
                <strong>{c.account}</strong>
                {c.reach.account_type ? <span className="small muted"> ({c.reach.account_type})</span> : null}
                {" · "}
                <span className="small">
                  {c.reach.selection === "all"
                    ? "all repositories"
                    : c.reach.repos
                      ? `${c.reach.repos.length} repositories: ${c.reach.repos.join(", ")}`
                      : ""}
                  {c.reach.mode ? `${c.reach.mode} mode: a deploy role and a read-only role for agents` : ""}
                  {c.reach.sites ? `Jira sites: ${c.reach.sites.map((x) => x.name).join(", ")}` : ""}
                  {c.reach.org ? `Sentry organization ${c.reach.org}` : ""}
                  {c.reach.subdomain ? `PagerDuty account ${c.reach.subdomain}` : ""}
                  {c.reach.user && !c.reach.subdomain ? `as ${c.reach.user}` : ""}
                </span>
                <div className="small muted">
                  Connected by {c.connected_by} on {when(c.connected_at)}; last worked {when(c.last_ok)}.
                </div>
                {c.status === "needs_attention" && (
                  <p className="error-note">
                    {c.last_error.charAt(0).toUpperCase() + c.last_error.slice(1)}.
                    {admin && t.available ? " Reconnect to fix it." : ""}
                  </p>
                )}
                {admin && (
                  <button className="small-btn" onClick={() => disconnect(t.tool, c.account, t.name)}>
                    Disconnect
                  </button>
                )}
              </div>
            ))}
            {t.tool === "aws" && admin && t.available && (
              <AwsConnectForm
                ws={ws}
                onDone={(m) => {
                  setNote(m);
                  setErr("");
                  load();
                }}
                onError={setErr}
              />
            )}
            {t.tool === "gcp" && admin && t.available && (
              <GcpConnectForm
                ws={ws}
                onDone={(m) => {
                  setNote(m);
                  setErr("");
                  load();
                }}
                onError={setErr}
              />
            )}
          </section>
        );
      })}
    </>
  );
}

const REGIONS = ["us-east-1", "us-east-2", "us-west-2", "ap-south-1", "eu-west-1"];

/** Connect AWS in two steps: create the stack in the customer's console, then let Valxel check its roles. */
function AwsConnectForm({
  ws,
  onDone,
  onError,
}: {
  ws: string;
  onDone: (msg: string) => void;
  onError: (msg: string) => void;
}) {
  const [mode, setMode] = useState("lite");
  const [region, setRegion] = useState("us-east-1");
  const [account, setAccount] = useState("");
  const [opened, setOpened] = useState(false);
  const [busy, setBusy] = useState(false);
  async function open() {
    try {
      const r = await api.awsLaunch(ws, mode, region);
      window.open(r.url, "_blank", "noopener");
      setOpened(true);
    } catch (e) {
      onError((e as Error).message);
    }
  }
  async function check(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const c = await api.awsCheck(ws, account, mode);
      onDone(`Connected AWS account ${c.account}.`);
    } catch (ex) {
      onError((ex as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="pad stack">
      <ol className="plain-ol">
        <li>
          <div className="grid-2">
            <label className="field">
              Mode
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="lite">Lite: products run on VMs</option>
                <option value="full">Full: EKS clusters and managed services</option>
              </select>
            </label>
            <label className="field">
              Region
              <select value={region} onChange={(e) => setRegion(e.target.value)}>
                {REGIONS.map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
            </label>
          </div>
          <button className="btn" type="button" onClick={open}>
            Open AWS to create the stack
          </button>
          <span className="small muted">
            {" "}
            Signed in to your AWS account, tick the IAM acknowledgement and choose Create stack.
          </span>
        </li>
        <li>
          <form onSubmit={check} className="submit-row">
            <label className="field">
              Your AWS account ID
              <input
                value={account}
                onChange={(e) => setAccount(e.target.value)}
                placeholder="123456789012"
                className="mono"
                inputMode="numeric"
                required
              />
            </label>
            <button
              className={opened ? "btn" : "btn secondary"}
              type="submit"
              disabled={busy || account.replace(/-/g, "").length !== 12}
            >
              {busy ? "Checking…" : "Check the connection"}
            </button>
          </form>
        </li>
      </ol>
    </div>
  );
}

const ACTIONS: Record<string, string> = {
  "workspace.create": "created the workspace",
  "member.add": "added someone",
  "member.accept": "accepted an invitation",
  "member.update": "changed someone's profile",
  "member.remove": "removed someone",
  "member.reinvite": "sent a new invitation link",
  "group.create": "added a group",
  "group.members": "changed a group's members",
  "group.delete": "deleted a group",
  "gates.set": "changed gate approvers",
  "connection.add": "connected a tool",
  "connection.reconnect": "reconnected a tool",
  "connection.remove": "disconnected a tool",
  "connection.failed": "found a connection broken",
  "connection.recovered": "found a connection working again",
};

function Audit({ ws }: { ws: string }) {
  const [rows, setRows] = useState<AuditEntry[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api
      .workspaceAudit(ws)
      .then(setRows)
      .catch((e) => setErr(e.message));
  }, [ws]);
  if (err)
    return (
      <p className="error-note" role="alert">
        {err}
      </p>
    );
  if (!rows) return null;
  return (
    <section className="panel" aria-labelledby="h-audit">
      <h2 id="h-audit" className="section-title">
        Audit log
      </h2>
      <ul className="ext-list">
        {rows.map((a, i) => (
          <li key={i}>
            <span className="small muted mono">{a.at.slice(0, 16).replace("T", " ")}</span>{" "}
            <strong>{a.actor}</strong> {ACTIONS[a.action] ?? a.action}
            {a.detail && Object.keys(a.detail).length > 0 && (
              <span className="small muted">
                {" "}
                ·{" "}
                {Object.entries(a.detail)
                  .map(
                    ([k, v]) =>
                      `${k}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v)}`,
                  )
                  .join("; ")}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Where an invitation link lands: accept it as the person signed in. */
export function JoinPage({ onJoined }: { onJoined: () => void }) {
  const { ws = "", token = "" } = useParams();
  const [state, setState] = useState<"ready" | "busy" | "done">("ready");
  const [err, setErr] = useState("");
  const nav = useNavigate();
  async function accept() {
    setState("busy");
    try {
      await api.join(ws, token);
      setState("done");
      onJoined();
      nav(`/workspaces/${ws}`);
    } catch (e) {
      setErr((e as Error).message);
      setState("ready");
    }
  }
  return (
    <section className="panel form-panel">
      <h1 className="pad">Join the {ws} workspace</h1>
      <p className="pad">
        Accepting adds you with the role and products the invitation gives. The link works once.
      </p>
      {err && (
        <p className="error-note pad" role="alert">
          {err}
        </p>
      )}
      <div className="submit-row pad">
        <button className="btn" disabled={state !== "ready"} onClick={accept}>
          Accept the invitation
        </button>
      </div>
    </section>
  );
}
