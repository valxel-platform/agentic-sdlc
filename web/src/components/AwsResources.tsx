import type { AwsResources } from "../api";

const VERB: Record<string, string> = { read: "reads", write: "writes", delete: "deletes" };
const usd = (n: number) => `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

type Firing = NonNullable<import("../api").PendingItem["alarms_firing"]>;

/** CloudWatch alarms firing in an earlier environment on what the feature touches. */
export function AlarmsFiring({ alarms }: { alarms: Firing }) {
  return (
    <ul className="ext-list">
      {alarms.map((a) => (
        <li key={`${a.env}-${a.resource}-${a.alarm}`}>
          <strong>{a.title}</strong> in {a.env}, {a.severity.toUpperCase()}, since {new Date(a.since).toLocaleString()}
          <div className="small muted">{a.says}</div>
          {a.url && <a className="small" href={a.url} target="_blank" rel="noreferrer">Open in CloudWatch</a>}
        </li>
      ))}
    </ul>
  );
}

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
      {aws.resources.some((r) => r.alarms && r.alarms.length > 0) && (
        <p className="small muted">
          Alarms that come with them, and open an incident in production:{" "}
          {aws.resources.filter((r) => r.alarms && r.alarms.length > 0)
            .map((r) => `${r.name} (${r.alarms!.map((a) => a.name.replace(/-/g, " ")).join(", ")})`).join("; ")}.
        </p>
      )}
    </div>
  );
}
