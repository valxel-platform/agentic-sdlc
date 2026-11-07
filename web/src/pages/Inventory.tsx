import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Inventory, type InventoryItem, type Me } from "../api";
import { money, threadPath } from "../vocab";

const KIND: Record<string, string> = { component: "Component", resource: "AWS or managed service", store_app: "Store app" };
const STATE_CHIP: Record<string, string> = { ready: "go", released: "go", progressing: "wait", failed: "stop" };

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="btn secondary small" aria-label={`Copy ${label}`}
      onClick={() => navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export function InventoryPage({ me }: { me: Me }) {
  const [params, setParams] = useSearchParams();
  const productId = params.get("product") || me.lanes[0]?.id || "";
  const [inv, setInv] = useState<Inventory | null>(null);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState("");

  const load = useCallback(() => {
    if (!productId) return;
    setInv(null);
    api.inventory(productId).then((r) => { setInv(r); setErr(""); }).catch((e) => setErr(e.message));
  }, [productId]);
  useEffect(load, [load]);

  if (!productId) return <p className="muted">You aren't a member of any product's organization.</p>;
  const groups = inv ? group(inv.items) : [];
  const prodEnv = inv?.pipeline[inv.pipeline.length - 1] ?? "production";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Inventory</h1>
          <p className="muted">What each environment runs, read live from OpenChoreo{inv?.aws === "checked" ? " and AWS" : ""}, with what you need to find it.</p>
        </div>
        <div className="head-actions">
          {me.lanes.length > 1 && (
            <select aria-label="Product" value={productId} onChange={(e) => setParams({ product: e.target.value })}>
              {me.lanes.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          )}
          <button type="button" className="btn secondary" onClick={load}>Refresh</button>
          <a className="btn secondary" href={`/api/inventory/${encodeURIComponent(productId)}/export.csv`}>Download CSV</a>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {!inv && !err && <p className="faint">Reading OpenChoreo…</p>}
      {inv && (
        <>
          <section className="panel" aria-labelledby="h-envs">
            <h2 id="h-envs" className="section-title">Production and the environments before it</h2>
            <div className="table-scroll">
            <table className="quality">
              <thead><tr>
                <th scope="col">Environment</th><th scope="col" className="num">Components</th>
                <th scope="col" className="num">Services</th><th scope="col" className="num">Store apps</th>
                <th scope="col" className="num">Not ready</th><th scope="col" className="num">Compute, last 30 days</th>
                <th scope="col" className="num">AWS, planned a month</th>
              </tr></thead>
              <tbody>
                {inv.pipeline.map((e) => {
                  const s = inv.summary.per_env[e];
                  if (!s) return null;
                  return (
                    <tr key={e} className={e === prodEnv ? "row-strong" : undefined}>
                      <th scope="row">{e}</th>
                      <td className="num">{s.components}</td><td className="num">{s.resources}</td><td className="num">{s.store_apps}</td>
                      <td className="num">{s.not_ready ? <span className="chip stop">{s.not_ready}</span> : 0}</td>
                      <td className="num">{money(s.cost_30d_usd)}</td><td className="num">{money(s.planned_monthly_usd)}</td>
                    </tr>
                  );
                })}
                <tr>
                  <th scope="row" className="muted">Before production, together</th>
                  <td className="num">{inv.summary.lower.components}</td><td className="num">{inv.summary.lower.resources}</td>
                  <td className="num">{inv.summary.lower.store_apps}</td><td />
                  <td className="num">{money(inv.summary.lower.cost_30d_usd)}</td><td className="num">{money(inv.summary.lower.planned_monthly_usd)}</td>
                </tr>
              </tbody>
            </table>
            </div>
            <p className="small muted pad" style={{ paddingTop: 8 }}>
              OpenChoreo project <span className="mono">{inv.project}</span> in namespace <span className="mono">{inv.namespace}</span>.
              {inv.aws !== "checked" && <> AWS accounts: {inv.aws}.</>}
            </p>
          </section>

          {inv.unmanaged.length > 0 && (
            <section className="panel" aria-labelledby="h-unmanaged" style={{ marginTop: 20 }}>
              <h2 id="h-unmanaged" className="section-title">In AWS, but nobody manages it</h2>
              <p className="small muted pad">Tagged for {inv.product_id} but unknown to OpenChoreo: adopt it into a feature's design, or remove it.</p>
              <ul className="ext-list">
                {inv.unmanaged.map((u) => (
                  <li key={u.arn}>
                    <div className="work-line"><span className="mono small">{u.arn}</span> <Copy text={u.arn} label="ARN" /></div>
                    <div className="small muted">{u.env}, account {u.account}{u.feature && <>, tagged with {u.feature}</>}</div>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {inv.problems.length > 0 && (
            <div className="note" role="status" style={{ marginTop: 20 }}>
              Some of it couldn't be read: {inv.problems.join("; ")}
            </div>
          )}

          <section className="panel" aria-labelledby="h-items" style={{ marginTop: 20 }}>
            <h2 id="h-items" className="section-title">What runs where</h2>
            {groups.length === 0 ? <p className="small muted pad">Nothing deployed yet.</p> : (
              <div className="table-scroll">
                <table className="quality">
                  <thead><tr>
                    <th scope="col">Name</th>
                    {inv.pipeline.map((e) => <th key={e} scope="col">{e}</th>)}
                  </tr></thead>
                  <tbody>
                    {groups.map((g) => (
                      <Group key={g.key} g={g} pipeline={inv.pipeline} open={open === g.key}
                        toggle={() => setOpen(open === g.key ? "" : g.key)} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}

interface ItemGroup { key: string; kind: InventoryItem["kind"]; name: string; type?: string; feature_id: string;
                    byEnv: Record<string, InventoryItem> }

function group(items: InventoryItem[]): ItemGroup[] {
  const out = new Map<string, ItemGroup>();
  for (const i of items) {
    const key = `${i.kind}:${i.name}`;
    const g = out.get(key) ?? { key, kind: i.kind, name: i.name, type: i.type, feature_id: i.feature_id, byEnv: {} };
    g.byEnv[i.env] = i;
    out.set(key, g);
  }
  return [...out.values()];
}

function Group({ g, pipeline, open, toggle }: { g: ItemGroup; pipeline: string[]; open: boolean; toggle: () => void }) {
  return (
    <>
      <tr>
        <th scope="row">
          <button type="button" className="textlink" aria-expanded={open} onClick={toggle}>{g.name}</button>
          <div className="small faint">{KIND[g.kind]}{g.type ? `, ${g.type}` : ""}</div>
        </th>
        {pipeline.map((e) => {
          const i = g.byEnv[e];
          return (
            <td key={e}>
              {i ? (
                <>
                  <span className={`chip ${STATE_CHIP[i.state] ?? "plain"}`}>{i.state}</span>
                  <div className="small mono">{i.release}{i.size ? `, ${i.size}` : ""}</div>
                </>
              ) : <span className="faint">–</span>}
            </td>
          );
        })}
      </tr>
      {open && (
        <tr className="detail-row">
          <td colSpan={pipeline.length + 1}>
            {g.feature_id && <p className="small pad" style={{ paddingTop: 8 }}>Created by <Link to={threadPath(g.feature_id)}>{g.feature_id}</Link></p>}
            {pipeline.filter((e) => g.byEnv[e]).map((e) => <Ids key={e} i={g.byEnv[e]} />)}
          </td>
        </tr>
      )}
    </>
  );
}

function Ids({ i }: { i: InventoryItem }) {
  return (
    <dl className="kv small">
      <dt><strong>{i.env}</strong></dt><dd />
      {i.ids.binding && <><dt>OpenChoreo</dt><dd className="mono">{i.ids.openchoreo}, binding {i.ids.binding}</dd></>}
      {i.ids.aws_account && <><dt>AWS</dt><dd className="mono">account {i.ids.aws_account}, {i.ids.aws_region}</dd></>}
      {i.ids.arn && <><dt>ARN</dt><dd className="mono">{i.ids.arn} <Copy text={i.ids.arn} label="ARN" /></dd></>}
      {(i.outputs?.length ?? 0) > 0 && <><dt>Outputs</dt><dd className="mono">{i.outputs!.map((o) => `${o.name}=${o.value}`).join(", ")}</dd></>}
      {(i.secrets?.length ?? 0) > 0 && (
        <><dt>Secrets</dt><dd>{i.secrets!.map((s) => <span key={s.output}>{s.output}: in Secret <span className="mono">{s.name}</span> ({s.key}) </span>)}</dd></>
      )}
      {i.ids.kubectl && <><dt>In the cluster</dt><dd className="mono">{i.ids.kubectl} <Copy text={i.ids.kubectl} label="kubectl command" /></dd></>}
      {i.cost_30d_usd ? <><dt>Last 30 days</dt><dd>{money(i.cost_30d_usd)}</dd></> : null}
      {i.planned_monthly_usd ? <><dt>Planned</dt><dd>{money(i.planned_monthly_usd)} a month</dd></> : null}
    </dl>
  );
}
