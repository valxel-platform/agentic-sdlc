import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import type { LiveEvent } from "./live";

// RTL does not auto-cleanup under vitest with globals: false.
afterEach(cleanup);

// Guard against a loaded shared box timing a test out.
vi.setConfig({ testTimeout: 30000 });

// The setup stub never connects, so tests install a controllable EventSource
// and re-import live.ts per test for fresh module state (one shared source,
// listener set, connection flag).

class FakeSource {
  static instances: FakeSource[] = [];
  url: string;
  withCredentials: boolean;
  readyState = 1;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;

  constructor(url: string, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = init?.withCredentials ?? false;
    FakeSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  fail(): void {
    this.onerror?.(new Event("error"));
  }

  emit(ev: LiveEvent): void {
    this.onmessage?.({ data: JSON.stringify(ev) } as MessageEvent);
  }

  emitRaw(data: string): void {
    this.onmessage?.({ data } as MessageEvent);
  }
}

const origEventSource = globalThis.EventSource;

type LiveModule = typeof import("./live");

async function freshLive(): Promise<LiveModule> {
  vi.resetModules();
  return import("./live");
}

function Probe({
  live,
  refresh,
  filter,
  showStatus = false,
}: {
  live: LiveModule;
  refresh: () => void;
  filter?: (e: LiveEvent) => boolean;
  showStatus?: boolean;
}) {
  live.useLive(refresh, filter);
  const connected = live.useLiveStatus();
  return <div>{showStatus ? (connected ? "connected" : "reconnecting") : "probe"}</div>;
}

beforeEach(() => {
  FakeSource.instances = [];
  globalThis.EventSource = FakeSource as unknown as typeof EventSource;
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  globalThis.EventSource = origEventSource;
  vi.restoreAllMocks();
});

function emit(ev: LiveEvent): void {
  FakeSource.instances[0]?.emit(ev);
}

function advance(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

describe("useLive", () => {
  it("opens one shared EventSource to /api/events with credentials", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    render(<Probe live={live} refresh={refresh} />);
    render(<Probe live={live} refresh={refresh} />);
    expect(FakeSource.instances).toHaveLength(1);
    expect(FakeSource.instances[0].url).toBe("/api/events");
    expect(FakeSource.instances[0].withCredentials).toBe(true);
  });

  it("calls refresh 250 ms after a matching event", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    render(<Probe live={live} refresh={refresh} />);
    emit({ type: "feature.updated", at: "2026-11-18T12:00:00Z" });
    expect(refresh).not.toHaveBeenCalled();
    advance(249);
    expect(refresh).not.toHaveBeenCalled();
    advance(1);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("debounces rapid events into a single refresh", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    render(<Probe live={live} refresh={refresh} />);
    emit({ type: "feature.updated", at: "2026-11-18T12:00:00Z" });
    advance(100);
    emit({ type: "feature.updated", at: "2026-11-18T12:00:01Z" });
    advance(100);
    emit({ type: "feature.updated", at: "2026-11-18T12:00:02Z" });
    advance(249);
    expect(refresh).not.toHaveBeenCalled();
    advance(1);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("honours the filter and ignores other events", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    render(<Probe live={live} refresh={refresh} filter={(e) => e.type === "feature.updated"} />);
    emit({ type: "signal.unmatched", at: "2026-11-18T12:00:00Z" });
    advance(300);
    expect(refresh).not.toHaveBeenCalled();
    emit({ type: "feature.updated", at: "2026-11-18T12:00:01Z" });
    advance(300);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("ignores malformed messages without throwing", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    render(<Probe live={live} refresh={refresh} />);
    expect(() => FakeSource.instances[0]?.emitRaw("not json{")).not.toThrow();
    advance(300);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("stops refreshing after unmount", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    const { unmount } = render(<Probe live={live} refresh={refresh} />);
    unmount();
    emit({ type: "feature.updated", at: "2026-11-18T12:00:00Z" });
    advance(300);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("uses the latest refresh callback after a rerender", async () => {
    const live = await freshLive();
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Probe live={live} refresh={first} />);
    rerender(<Probe live={live} refresh={second} />);
    emit({ type: "feature.updated", at: "2026-11-18T12:00:00Z" });
    advance(300);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("cancels a pending refresh on unmount", async () => {
    const live = await freshLive();
    const refresh = vi.fn();
    const { unmount } = render(<Probe live={live} refresh={refresh} />);
    emit({ type: "feature.updated", at: "2026-11-18T12:00:00Z" });
    advance(100);
    unmount();
    advance(500);
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("useLiveStatus", () => {
  it("starts reconnecting, goes live on open, and back on error", async () => {
    const live = await freshLive();
    render(<Probe live={live} refresh={() => {}} showStatus />);
    expect(screen.getByText("reconnecting")).toBeInTheDocument();
    act(() => {
      FakeSource.instances[0]?.open();
    });
    expect(screen.getByText("connected")).toBeInTheDocument();
    act(() => {
      FakeSource.instances[0]?.fail();
    });
    expect(screen.getByText("reconnecting")).toBeInTheDocument();
  });

  it("shares the connection flag between subscribers", async () => {
    const live = await freshLive();
    render(
      <>
        <Probe live={live} refresh={() => {}} showStatus />
        <Probe live={live} refresh={() => {}} showStatus />
      </>,
    );
    expect(screen.getAllByText("reconnecting")).toHaveLength(2);
    act(() => {
      FakeSource.instances[0]?.open();
    });
    expect(screen.getAllByText("connected")).toHaveLength(2);
  });
});

describe("closeLive", () => {
  it("closes the shared source", async () => {
    const live = await freshLive();
    render(<Probe live={live} refresh={() => {}} />);
    expect(FakeSource.instances).toHaveLength(1);
    live.closeLive();
    expect(FakeSource.instances[0].closed).toBe(true);
  });

  it("is a no-op before anything connects", async () => {
    const live = await freshLive();
    expect(() => live.closeLive()).not.toThrow();
    expect(FakeSource.instances).toHaveLength(0);
  });

  it("lets the next subscriber open a fresh source", async () => {
    const live = await freshLive();
    const { unmount } = render(<Probe live={live} refresh={() => {}} />);
    live.closeLive();
    unmount();
    render(<Probe live={live} refresh={() => {}} />);
    expect(FakeSource.instances).toHaveLength(2);
    expect(FakeSource.instances[1].closed).toBe(false);
  });
});
