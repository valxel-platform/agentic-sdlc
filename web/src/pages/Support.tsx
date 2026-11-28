import { useCallback, useEffect, useState } from "react";
import { api, type SupportDetail, type SupportLink, type SupportProduct } from "../api";

/**
 * Support — a product's dashboards, logs and traces (Grafana) and its agents' traces (MLflow),
 * opened from here only. The tool signs the person in for that one product (valxel_sdlc.support):
 * its members and the workspace's Support group view, admins edit, and MLflow is open only to the
 * groups an admin grants it.
 */
export function SupportPage() {
  const [products, setProducts] = useState<SupportProduct[] | null>(null);
  const [configured, setConfigured] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    api
      .support()
      .then((r) => {
        setProducts(r.products);
        setConfigured(r.configured);
      })
      .catch((e) => setErr(e.message));
  }, []);

  if (err)
    return (
      <p className="error-note" role="alert">
        {err}
      </p>
    );
  if (!products) return <p className="faint">Loading…</p>;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Support</h1>
          <p className="muted">
            Open a product's dashboards and traces. You're signed in for that product only, and each opens in
            a new window.
          </p>
        </div>
      </div>
      {!configured && (
        <p className="small muted pad" role="status">
          Sign-in for Grafana and MLflow isn't set up in this installation yet, so the links won't open.
        </p>
      )}
      {products.length === 0 ? (
        <div className="panel empty">
          <h2>No products to support</h2>
          <p>Ask a workspace admin to add you to the product, or to the workspace's Support group.</p>
        </div>
      ) : (
        products.map((p) => <ProductSupport key={p.product} product={p} />)
      )}
    </>
  );
}

function ProductSupport({ product }: { product: SupportProduct }) {
  const [detail, setDetail] = useState<SupportDetail | null>(null);
  const load = useCallback(() => {
    api
      .supportProduct(product.product)
      .then(setDetail)
      .catch(() => setDetail(null));
  }, [product.product]);
  useEffect(load, [load]);
  const links = detail?.links ?? product.links;

  return (
    <section className="panel" aria-label={`Support for ${product.name}`}>
      <div className="pad">
        <h2>
          {product.name} <span className="small muted">{product.product}</span>
        </h2>
        {links.length === 0 ? (
          <p className="small muted">Grafana and MLflow aren't connected for this product yet.</p>
        ) : (
          <ul className="support-links">
            {links.map((l) => (
              <li key={l.href}>
                <OpenLink link={l} />
              </li>
            ))}
          </ul>
        )}
        {detail && !detail.mlflow && (
          <p className="small muted">
            Agent traces can include prompts and tool data, so they open only for groups a workspace admin
            allows.
          </p>
        )}
        {detail?.settings && <TraceAccess productId={product.product} detail={detail} onSaved={load} />}
      </div>
    </section>
  );
}

function OpenLink({ link }: { link: SupportLink }) {
  return (
    <>
      <a className="btn" href={link.href} target="_blank" rel="noopener noreferrer">
        Open {link.label.toLowerCase()}
      </a>{" "}
      <span className="small muted">
        {link.detail}
        {link.role ? `. You'll be a ${link.role === "Editor" ? "editor" : "viewer"}.` : "."}
      </span>
    </>
  );
}

/** Admins only: which of the workspace's groups may open the product's agent traces. */
function TraceAccess({
  productId,
  detail,
  onSaved,
}: {
  productId: string;
  detail: SupportDetail;
  onSaved: () => void;
}) {
  const settings = detail.settings!;
  const [chosen, setChosen] = useState<string[]>(settings.mlflow_groups);
  const [msg, setMsg] = useState("");
  const changed = [...chosen].sort().join() !== [...settings.mlflow_groups].sort().join();

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setMsg("");
    try {
      await api.setSupport(productId, chosen);
      setMsg("Saved. People in these groups can open agent traces from their next sign-in.");
      onSaved();
    } catch (ex) {
      setMsg((ex as Error).message);
    }
  }

  return (
    <form onSubmit={save}>
      <fieldset className="field">
        <legend>
          Who may open agent traces <span className="small muted">none ticked: nobody</span>
        </legend>
        {settings.groups.map((g) => (
          <label key={g.slug} className="check">
            <input
              type="checkbox"
              checked={chosen.includes(g.slug)}
              onChange={(e) =>
                setChosen(e.target.checked ? [...chosen, g.slug] : chosen.filter((x) => x !== g.slug))
              }
            />{" "}
            {g.name}
          </label>
        ))}
      </fieldset>
      <div className="submit-row">
        <button className="btn" type="submit" disabled={!changed}>
          Save trace access
        </button>
        {msg && (
          <span className="small muted" role="status">
            {msg}
          </span>
        )}
      </div>
    </form>
  );
}
