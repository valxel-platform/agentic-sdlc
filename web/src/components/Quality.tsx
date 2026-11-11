import type { QualitySuite, Waiver } from "../api";

const STAGE: Record<string, string> = { pr: "Pull requests", build: "Build", change_set: "Before merge", dev: "Dev", staging: "Staging", production: "Production" };
const STATE: Record<string, [string, string]> = {
  passed: ["Passed", "go"], failed: ["Failed", "stop"], pending: ["Running", "run"], missing: ["No result", "plain"],
};
const SUITE: Record<string, string> = {
  unit: "Unit tests", contract: "Contract tests", integration: "Integration tests", e2e: "End-to-end tests",
  smoke: "Smoke tests", sast: "Code scanning", dependencies: "Dependency review", secrets: "Secret scanning",
  iac: "Infrastructure scan", container: "Image scan", dast: "Dynamic scan",
};

function count(x: unknown): number {
  return Array.isArray(x) ? x.length : typeof x === "number" ? x : 0;
}

/** One row per suite and stage, folding repos together: the worst state wins. */
function fold(suites: QualitySuite[]) {
  const order = ["failed", "missing", "pending", "passed"];
  const rows = new Map<string, QualitySuite & { repos: number; findingCount: number; waivedCount: number; minCov?: number }>();
  for (const s of suites) {
    const key = `${s.stage ?? ""}/${s.suite}`;
    const prev = rows.get(key);
    const cov = s.coverage ?? undefined;
    if (!prev) {
      rows.set(key, { ...s, repos: 1, findingCount: count(s.findings), waivedCount: count(s.waived), minCov: cov });
      continue;
    }
    prev.repos += 1;
    prev.findingCount += count(s.findings);
    prev.waivedCount += count(s.waived);
    if (order.indexOf(s.state) < order.indexOf(prev.state)) prev.state = s.state;
    if (cov != null) prev.minCov = prev.minCov == null ? cov : Math.min(prev.minCov, cov);
    prev.passed = (prev.passed ?? 0) + (s.passed ?? 0);
    prev.failed = (prev.failed ?? 0) + (s.failed ?? 0);
  }
  const stages = ["pr", "build", "change_set", "dev", "staging", "production"];
  return [...rows.values()].sort((a, b) =>
    stages.indexOf(a.stage ?? "") - stages.indexOf(b.stage ?? "") ||
    (a.kind === b.kind ? 0 : a.kind === "test" ? -1 : 1) || a.suite.localeCompare(b.suite));
}

export function QualityTable({ suites, waivers }: { suites: QualitySuite[]; waivers?: Waiver[] }) {
  const rows = fold(suites);
  if (!rows.length) return <p className="muted small">No tests or scans have reported yet.</p>;
  return (
    <>
      <table className="quality">
        <thead>
          <tr><th scope="col">Stage</th><th scope="col">Suite</th><th scope="col">Result</th><th scope="col">Detail</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const [label, tone] = STATE[r.state] ?? [r.state, "plain"];
            const detail = r.kind === "test"
              ? [r.passed != null ? `${r.passed} passed, ${r.failed ?? 0} failed` : "", r.minCov != null ? `coverage ${r.minCov.toFixed(0)}%` : ""]
              : [`${r.findingCount} finding${r.findingCount === 1 ? "" : "s"}`, r.waivedCount ? `${r.waivedCount} accepted` : ""];
            return (
              <tr key={`${r.stage}/${r.suite}`}>
                <td className="muted">{STAGE[r.stage ?? ""] ?? r.stage}</td>
                <td>{SUITE[r.suite] ?? r.suite}{r.required === false ? <span className="faint small"> optional</span> : null}</td>
                <td><span className={`chip ${tone}`}>{label}</span></td>
                <td className="small muted">{detail.filter(Boolean).join(", ")}{r.repos > 1 ? ` across ${r.repos} repos` : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {waivers && waivers.length > 0 && (
        <div className="waivers">
          <h3>Accepted risks</h3>
          <ul>
            {waivers.map((w) => (
              <li key={w.fingerprint} className="small">
                <strong>{w.severity}</strong> {w.title} ({w.repo.split("/").pop()}): {w.reason}. Accepted by {w.approver} until {w.expires.slice(0, 10)}.
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
