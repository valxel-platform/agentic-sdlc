import type { CostCategory } from "../api";
import { money } from "../vocab";

// How sure a figure is, in words a reader can weigh.
const BASIS: Record<string, string> = {
  metered: "usage at list price", billed: "from the AWS bill", allocated: "shared out", estimate: "estimate",
};

export function CostTable({ rows, total, caption }: { rows: CostCategory[]; total: number; caption: string }) {
  if (rows.length === 0) return <p className="small muted" style={{ padding: "8px 16px 16px" }}>Nothing recorded yet.</p>;
  return (
    <table className="quality">
      <caption className="visually-hidden">{caption}</caption>
      <thead><tr><th scope="col">Spent on</th><th scope="col">How it's counted</th><th scope="col" className="num">Cost</th></tr></thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.category}>
            <td>{c.label}</td>
            <td className="small muted">{c.basis.map((b) => BASIS[b] ?? b).join(", ")}{c.provisional && ", may still change"}</td>
            <td className="num">{money(c.usd)}</td>
          </tr>
        ))}
      </tbody>
      <tfoot><tr><th scope="row" colSpan={2}>Total</th><td className="num"><strong>{money(total)}</strong></td></tr></tfoot>
    </table>
  );
}
