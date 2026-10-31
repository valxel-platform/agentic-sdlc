import { useEffect, useRef, useState } from "react";

export interface LiveEvent { type: string; feature_id?: string; node?: string; at: string }

type Listener = (e: LiveEvent) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;
let connected = false;
const statusListeners = new Set<(c: boolean) => void>();

function ensure() {
  if (source) return;
  source = new EventSource("/api/events", { withCredentials: true });
  source.onopen = () => { connected = true; statusListeners.forEach((f) => f(true)); };
  source.onerror = () => { connected = false; statusListeners.forEach((f) => f(false)); };
  source.onmessage = (m) => {
    try {
      const ev = JSON.parse(m.data) as LiveEvent;
      listeners.forEach((f) => f(ev));
    } catch { /* ignore malformed */ }
  };
}

export function closeLive() {
  source?.close();
  source = null;
  connected = false;
}

/** Call `refresh` (debounced) whenever a live event matches `filter`. */
export function useLive(refresh: () => void, filter: (e: LiveEvent) => boolean = () => true) {
  const ref = useRef({ refresh, filter });
  ref.current = { refresh, filter };
  useEffect(() => {
    ensure();
    let t: number | undefined;
    const fn: Listener = (e) => {
      if (!ref.current.filter(e)) return;
      window.clearTimeout(t);
      t = window.setTimeout(() => ref.current.refresh(), 250);
    };
    listeners.add(fn);
    return () => { listeners.delete(fn); window.clearTimeout(t); };
  }, []);
}

export function useLiveStatus(): boolean {
  const [c, setC] = useState(connected);
  useEffect(() => {
    ensure();
    statusListeners.add(setC);
    return () => { statusListeners.delete(setC); };
  }, []);
  return c;
}
