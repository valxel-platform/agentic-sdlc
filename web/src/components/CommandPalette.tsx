import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type FeatureSummary, type Me, type PendingItem } from "../api";
import { threadPath } from "../vocab";

type Entry = { label: string; hint?: string; to: string; group: string };

const GO_TO: Entry[] = [
  { label: "Today", to: "/today", group: "Go to" },
  { label: "Approvals", to: "/inbox", group: "Go to" },
  { label: "Features", to: "/board", group: "Go to" },
  { label: "Products", to: "/products", group: "Go to" },
  { label: "Roadmap", to: "/roadmap", group: "Go to" },
  { label: "Incidents", to: "/incidents", group: "Go to" },
  { label: "Changes", to: "/changes", group: "Go to" },
  { label: "Costs", to: "/costs", group: "Go to" },
  { label: "Inventory", to: "/inventory", group: "Go to" },
  { label: "Lessons", to: "/lessons", group: "Go to" },
  { label: "Pilot", to: "/pilot", group: "Go to" },
  { label: "Connected apps", to: "/connected-apps", group: "Go to" },
  { label: "Activity", to: "/activity", group: "Go to" },
  { label: "Workspace admin", to: "/workspaces", group: "Go to" },
  { label: "Start a new product", to: "/products", group: "Actions" },
  { label: "Start a new request", to: "/intake", group: "Actions" },
];

/**
 * ⌘K / Ctrl+K palette. Client-side only: entries come from the fixed route
 * list plus already-fetched features and pending decisions — no search endpoint.
 */
export function CommandPalette({ open, onClose, me }: { open: boolean; onClose: () => void; me: Me }) {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [features, setFeatures] = useState<FeatureSummary[]>([]);
  const [pending, setPending] = useState<PendingItem[]>([]);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setSel(0);
    input.current?.focus();
    api
      .features()
      .then(setFeatures)
      .catch(() => {});
    api
      .inbox(true)
      .then(setPending)
      .catch(() => {});
  }, [open]);

  const entries = useMemo<Entry[]>(() => {
    const all: Entry[] = [...GO_TO];
    for (const f of features) {
      all.push({ label: f.title, hint: f.feature_id, to: `/features/${f.feature_id}`, group: "Features" });
    }
    for (const p of pending) {
      all.push({
        label: p.title ?? p.feature_id ?? "Decision",
        hint: p.feature_id,
        to: p.feature_id ? threadPath(p.feature_id) : "/inbox",
        group: "Approvals",
      });
    }
    for (const l of me.lanes) {
      all.push({ label: l.name, hint: l.id, to: "/products", group: "Products" });
    }
    return all;
  }, [features, pending, me.lanes]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter((e) => `${e.label} ${e.hint ?? ""}`.toLowerCase().includes(needle));
  }, [entries, q]);

  useEffect(() => {
    setSel(0);
  }, [q]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSel((s) => Math.min(filtered.length - 1, s + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSel((s) => Math.max(0, s - 1));
      } else if (e.key === "Enter" && filtered[sel]) {
        e.preventDefault();
        nav(filtered[sel].to);
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, filtered, sel, nav, onClose]);

  if (!open) return null;
  let lastGroup = "";
  return (
    <div className="palette-overlay" onClick={onClose} role="dialog" aria-label="Search and go to">
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <input
          ref={input}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search projects, approvals, apps…"
          aria-label="Search projects, approvals, apps"
          autoFocus
        />
        <div className="palette-list" role="listbox" aria-label="Results">
          {filtered.length === 0 && <p className="palette-empty">Nothing matches “{q}”.</p>}
          {filtered.map((e, i) => {
            const head = e.group !== lastGroup ? <div className="palette-group">{e.group}</div> : null;
            lastGroup = e.group;
            return (
              <div key={`${e.group}-${e.label}-${e.hint ?? ""}-${i}`}>
                {head}
                <button
                  type="button"
                  role="option"
                  aria-selected={i === sel}
                  className={i === sel ? "sel" : ""}
                  onMouseEnter={() => setSel(i)}
                  onClick={() => {
                    nav(e.to);
                    onClose();
                  }}
                >
                  <span>{e.label}</span>
                  {e.hint && <span className="hint">{e.hint}</span>}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
