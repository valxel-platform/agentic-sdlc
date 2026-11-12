import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type PendingItem } from "../api";
import { useLive } from "../live";
import { DecisionPanel } from "../components/Decision";
import { threadPath, waitingLabel } from "../vocab";

type Group = { key: string; product: string; name: string; sections: { initiative: string; items: PendingItem[] }[] };

/** Decisions grouped by product, then by initiative. */
function grouped(items: PendingItem[]): Group[] {
  const out: Group[] = [];
  for (const i of items) {
    let g = out.find((x) => x.product === i.product_id);
    if (!g) { g = { key: i.product_id ?? "", product: i.product_id ?? "", name: i.product_name ?? i.product_id ?? "", sections: [] }; out.push(g); }
    const ini = i.initiative_id ?? "";
    let s = g.sections.find((x) => x.initiative === ini);
    if (!s) { s = { initiative: ini, items: [] }; g.sections.push(s); }
    s.items.push(i);
  }
  out.forEach((g) => g.sections.sort((a, b) => (a.initiative ? 0 : 1) - (b.initiative ? 0 : 1)));
  return out;
}

export function Inbox() {
  const [mine, setMine] = useState(true);
  const [items, setItems] = useState<PendingItem[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState(false);  // on a phone: the decision instead of the list
  const [err, setErr] = useState("");
  const [batch, setBatch] = useState<{ approved: string[]; skipped: { feature_id: string; why: string }[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const opened = useRef(Date.now());

  const load = useCallback(() => {
    api.inbox(mine).then((list) => {
      setItems(list);
      setSelected((cur) => (cur && list.some((i) => i.interrupt_id === cur) ? cur : list[0]?.interrupt_id ?? null));
      setErr("");
    }).catch((e) => setErr(e.message));
  }, [mine]);
  useEffect(load, [load]);
  useLive(load, (e) => e.type === "feature.updated");

  const current = items?.find((i) => i.interrupt_id === selected);
  const easy = (items ?? []).filter((i) => i.low_risk && i.can_decide);

  async function approveEasy() {
    setBusy(true);
    try {
      const minutes = Math.min(120, (Date.now() - opened.current) / 60000);
      setBatch(await api.approveBatch(easy.map((i) => ({ feature_id: i.feature_id!, interrupt_id: i.interrupt_id })), minutes));
      load();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Decisions</h1>
          <p className="muted">{items ? `${items.length} waiting` : "Gates and escalations that need a person before agents can continue."}</p>
        </div>
        <div className="seg" role="group" aria-label="Show">
          <button aria-pressed={mine} onClick={() => setMine(true)}>Waiting on me</button>
          <button aria-pressed={!mine} onClick={() => setMine(false)}>Everyone</button>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {easy.length > 1 && (
        <div className="batch-bar" role="region" aria-label="Low-risk decisions">
          <span><strong>{easy.length} are low risk:</strong> the critic passed, no PHI or payments data, and nothing new
            is created. <span className="faint">{easy.map((i) => i.feature_id).join(", ")}</span></span>
          <button className="btn go" disabled={busy} onClick={approveEasy}>Approve these {easy.length}</button>
        </div>
      )}
      {batch && (
        <p className="note" role="status">Approved {batch.approved.length}.
          {batch.skipped.length > 0 && ` Left for one at a time: ${batch.skipped.map((s) => `${s.feature_id} (${s.why})`).join(", ")}.`}</p>
      )}
      {items && items.length === 0 ? (
        <div className="panel empty">
          <h2>Nothing is waiting on {mine ? "you" : "anyone"}</h2>
          <p>Agents are working, or every feature has finished. <Link to="/board">See all features</Link> or <Link to="/intake">start a new request</Link>.</p>
        </div>
      ) : (
        <div className={`inbox${focus ? " focused" : ""}`}>
          <div className="items" role="list">
            {grouped(items ?? []).map((g) => (
              <div key={g.key} className="inbox-group" role="presentation">
                <h2 className="group-head">{g.name}</h2>
                {g.sections.map((s) => (
                  <div key={s.initiative || "none"} role="presentation">
                    <p className="group-sub small faint">{s.initiative ? <Link to={threadPath(s.initiative)}>{s.initiative}</Link> : "Not in an initiative"}</p>
                    {s.items.map((i) => (
                      <button key={i.interrupt_id} role="listitem"
                        className={`item ${i.type === "escalation" ? "escalation" : ""} ${i.can_decide ? "" : "readonly"}`}
                        aria-current={i.interrupt_id === selected} onClick={() => { setSelected(i.interrupt_id); setFocus(true); }}>
                        <span className="t">{i.title ?? i.feature_id}</span>
                        <span className="small muted">{waitingLabel(i, i.product_id)}{i.low_risk && i.can_decide ? <span className="chip go low">Low risk</span> : null}</span>
                        <span className="small faint">{i.feature_id}{!i.can_decide ? ` for ${i.approvers.join(", ")}` : ""}</span>
                      </button>
                    ))}
                  </div>
                ))}
              </div>
            ))}
          </div>
          {current && current.feature_id && (
            <div className="inbox-decision">
              <button className="linkish back" onClick={() => setFocus(false)}>Back to all decisions</button>
              <DecisionPanel item={current} featureId={current.feature_id} onDone={() => { setFocus(false); load(); }} />
            </div>
          )}
        </div>
      )}
    </>
  );
}
