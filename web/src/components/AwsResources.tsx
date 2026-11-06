import type { AwsResources } from "../api";

const VERB: Record<string, string> = { read: "reads", write: "writes", delete: "deletes" };
const usd = (n: number) => `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

/** The AWS services a design asks for: who may use each, its size and planning cost per environment. */
export function AwsResourcesTable({ aws }: { aws: AwsResources }) {
  const envs = Object.keys(aws.monthly_usd);
  return (
    <div className="aws-resources">
      <table className="quality">
        <thead>
          <tr>
            <th scope="col">Service</th>
            <th scope="col">Who uses it</th>
            {envs.map((e) => <th scope="col" key={e}>{e}</th>)}
          </tr>
        </thead>
        <tbody>
          {aws.resources.map((r) => (
            <tr key={r.name}>
              <td>
                <strong>{r.name}</strong>
                <div className="small muted">{r.service}{r.stateful ? ", keeps data" : ""}</div>
              </td>
              <td className="small">
                {Object.entries(r.access).map(([c, a]) => `${c} ${a.map((v) => VERB[v] ?? v).join(" and ")}`).join("; ")}
              </td>
              {envs.map((e) => <td key={e} className="small">{r.sizes[e]}, {usd(r.monthly_usd[e])}/mo</td>)}
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={2}>Per month</th>
            {envs.map((e) => <td key={e}><strong>{usd(aws.monthly_usd[e])}</strong></td>)}
          </tr>
        </tfoot>
      </table>
      <p className="small muted">
        In {aws.region || "the product's region"}, each environment in its own AWS account. {aws.note}
      </p>
    </div>
  );
}
