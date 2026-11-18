import type { Station, StationState } from "../api";

/**
 * The seven-stage stepper (Idea → Plan → Design → Build → Check → Ship → Live),
 * derived from the backend Station[] — never hard-coded from a fixed list.
 * Architecture (G4) lives in Build, G5/code review in Check, G6/release in Ship.
 * Product-brief / product gates and express (skipped) stations fold into their
 * nearest stage; a stage with no stations at all renders as "todo".
 */
export const STAGE_LABELS = ["Idea", "Plan", "Design", "Build", "Check", "Ship", "Live"] as const;

const STAGE_KEYS: string[][] = [
  ["discovery", "g1"],
  ["spec", "g2"],
  ["design", "g3"],
  ["architecture", "plan", "build", "g4"],
  ["g5", "security"],
  ["release", "g6"],
  ["operate", "delivery"],
];

function stageState(stations: Station[], keys: string[]): StationState {
  const mine = stations.filter((s) => keys.includes(s.key));
  if (mine.length === 0) return "todo";
  if (mine.some((s) => s.state === "stopped")) return "stopped";
  if (mine.some((s) => s.state === "waiting")) return "waiting";
  if (mine.some((s) => s.state === "current")) return "current";
  if (mine.every((s) => s.state === "done" || s.state === "skipped")) return "done";
  return "todo";
}

export function stageStates(stations: Station[]): StationState[] {
  return STAGE_KEYS.map((keys) => stageState(stations, keys));
}

/** Current stage index (0-based) for a feature, or -1 when nothing has started. */
export function currentStage(stations: Station[]): number {
  const states = stageStates(stations);
  const cur = states.findIndex((s) => s === "current" || s === "waiting");
  if (cur >= 0) return cur;
  const done = states.map((s, i) => (s === "done" ? i : -1)).filter((i) => i >= 0);
  return done.length ? Math.min(6, Math.max(...done) + 1) : 0;
}

export function Stepper({ stations, mini = false }: { stations: Station[]; mini?: boolean }) {
  const states = stageStates(stations);
  return (
    <ol className={`stepper${mini ? " mini" : ""}`} aria-label="Where this is, from idea to live">
      {STAGE_LABELS.map((label, i) => (
        <li
          key={label}
          className={states[i]}
          aria-current={states[i] === "current" || states[i] === "waiting" ? "step" : undefined}
        >
          <span>{label}</span>
        </li>
      ))}
    </ol>
  );
}
