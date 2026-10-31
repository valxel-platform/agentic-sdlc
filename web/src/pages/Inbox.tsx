import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type PendingItem } from "../api";
import { useLive } from "../live";
import { DecisionPanel } from "../components/Decision";
import { waitingLabel } from "../vocab";

export function Inbox() {
  const [mine, setMine] = useState(true);
  const [items, setItems] = useState<PendingItem[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [err, setErr] = useState("");

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

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Decisions</h1>
          <p className="muted">Gates and escalations that need a person before agents can continue.</p>
        </div>
        <div className="seg" role="group" aria-label="Show">
          <button aria-pressed={mine} onClick={() => setMine(true)}>Waiting on me</button>
          <button aria-pressed={!mine} onClick={() => setMine(false)}>Everyone</button>
        </div>
      </div>
      {err && <p className="error-note" role="alert">{err}</p>}
      {items && items.length === 0 ? (
        <div className="panel empty">
          <h2>Nothing is waiting on {mine ? "you" : "anyone"}</h2>
          <p>Agents are working, or every feature has finished. <Link to="/board">See all features</Link> or <Link to="/intake">start a new intent</Link>.</p>
        </div>
      ) : (
        <div className="inbox">
          <div className="items" role="list">
            {items?.map((i) => (
              <button key={i.interrupt_id} role="listitem"
                className={`item ${i.type === "escalation" ? "escalation" : ""} ${i.can_decide ? "" : "readonly"}`}
                aria-current={i.interrupt_id === selected} onClick={() => setSelected(i.interrupt_id)}>
                <span className="t">{i.title ?? i.feature_id}</span>
                <span className="small muted">{waitingLabel(i, i.product_id)}</span>
                <span className="small faint">{i.feature_id}{!i.can_decide ? ` for ${i.approvers.join(", ")}` : ""}</span>
              </button>
            ))}
          </div>
          {current && current.feature_id && (
            <DecisionPanel item={current} featureId={current.feature_id} onDone={load} />
          )}
        </div>
      )}
    </>
  );
}
