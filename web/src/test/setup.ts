import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Vitest runs with globals: false, so Testing Library's automatic
// cleanup never registers; unmount every test's DOM explicitly.
afterEach(() => cleanup());

// jsdom has no matchMedia; Md.tsx reads it (guarded) and later tests need
// a deterministic value.
vi.stubGlobal(
  "matchMedia",
  (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList,
);

// live.ts opens one shared EventSource to /api/events. No test may hit the
// network, so the global is a stub that never connects; tests that need
// events replace it per-test.
class StubEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSED = 2;
  readyState = StubEventSource.OPEN;
  url: string;
  withCredentials: boolean;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(url: string | URL, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = init?.withCredentials ?? false;
  }

  addEventListener(): void {}
  removeEventListener(): void {}
  dispatchEvent(): boolean {
    return true;
  }
  close(): void {
    this.readyState = StubEventSource.CLOSED;
  }
}
vi.stubGlobal("EventSource", StubEventSource);

// jsdom does not implement scrollIntoView; Intake.tsx calls it in an effect.
Element.prototype.scrollIntoView = function (): void {};
