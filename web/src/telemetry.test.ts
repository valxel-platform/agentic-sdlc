import { context, trace } from "@opentelemetry/api";
import { InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { describe, expect, test, beforeAll, afterAll, afterEach, vi } from "vitest";
import { api } from "./api";
import {
  SERVICE_NAME,
  endpointIgnorePattern,
  recordError,
  sameOrigin,
  startTelemetry,
  stripQuery,
  traceHeaders,
  withoutQueryStrings,
} from "./telemetry";

const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$/;

describe("telemetry off", () => {
  test("no endpoint configured means tracing stays off", () => {
    expect(startTelemetry()).toBeNull();
  });

  test("an explicit off switch wins over a configured endpoint", () => {
    expect(startTelemetry({ enabled: false, endpoint: "https://otel.example.test/v1/traces" })).toBeNull();
  });

  test("traceHeaders is empty when tracing is off", () => {
    expect(traceHeaders()).toEqual({});
  });

  test("stripQuery keeps the path", () => {
    expect(stripQuery("https://x/a/b?q=1#f")).toBe("https://x/a/b");
    expect(stripQuery("https://x/a#f?q")).toBe("https://x/a");
    expect(stripQuery("/a")).toBe("/a");
  });

  test("withoutQueryStrings strips query strings from span URL attributes", () => {
    const setAttribute = vi.fn();
    const span = {
      attributes: { "url.full": "https://x/a?name=Ana", "http.url": "https://x/a?dob=1984" },
      setAttribute,
    };
    withoutQueryStrings(span as never);
    expect(setAttribute).toHaveBeenCalledWith("url.full", "https://x/a");
    expect(setAttribute).toHaveBeenCalledWith("http.url", "https://x/a");
  });

  test("withoutQueryStrings ignores spans with no URL attributes", () => {
    const setAttribute = vi.fn();
    withoutQueryStrings({ attributes: {}, setAttribute } as never);
    expect(setAttribute).not.toHaveBeenCalled();
  });

  test("endpointIgnorePattern matches the endpoint origin and nothing else", () => {
    const pattern = endpointIgnorePattern("https://otel.example.test:4318/v1/traces");
    expect(pattern).toBeDefined();
    expect(pattern?.test("https://otel.example.test:4318/v1/traces")).toBe(true);
    expect(pattern?.test("https://otel.example.test.evil.com/v1/traces")).toBe(false);
    expect(pattern?.test("https://other.example.test/v1/traces")).toBe(false);
  });

  test("the API's own /otlp/ endpoint is same-origin, so API calls keep their traceparent", () => {
    expect(sameOrigin(new URL("/otlp/v1/traces", window.location.origin).href)).toBe(true);
    expect(sameOrigin("https://otel.example.test:4318/v1/traces")).toBe(false);
    expect(sameOrigin("not a url")).toBe(false);
  });

  test("endpointIgnorePattern returns undefined for an invalid URL", () => {
    expect(endpointIgnorePattern("not a url")).toBeUndefined();
  });
});

describe("telemetry on", () => {
  const spans = new InMemorySpanExporter();
  const seen: Headers[] = [];

  beforeAll(() => {
    // FetchInstrumentation wraps the fetch present when it starts.
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        seen.push(new Headers(init?.headers));
        return Promise.resolve(new Response("{}", { status: 200 }));
      }),
    );
    // The endpoint comes from the environment, as it does in the real app:
    // no VITE_OTEL_EXPORTER_OTLP_ENDPOINT, no collector, no tracing.
    vi.stubEnv("VITE_OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.example.test/v1/traces");
    const provider = startTelemetry({ service: SERVICE_NAME, processor: new SimpleSpanProcessor(spans) });
    expect(provider).not.toBeNull();
  });

  afterEach(() => {
    spans.reset();
    seen.length = 0;
  });

  afterAll(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  test("API calls carry a traceparent and are recorded as spans", async () => {
    await fetch(`${window.location.origin}/api/v1/greetings?name=x`);
    expect(seen[0]?.get("traceparent")).toMatch(TRACEPARENT);
    await vi.waitFor(
      () => {
        expect(spans.getFinishedSpans().length).toBeGreaterThan(0);
      },
      { timeout: 2000 },
    );
    const [span] = spans.getFinishedSpans();
    expect(span?.resource.attributes["service.name"]).toBe(SERVICE_NAME);
    expect(span?.attributes["http.request.method"] ?? span?.attributes["http.method"]).toBe("GET");
  });

  test("query strings never reach a trace", async () => {
    await fetch(`${window.location.origin}/api/v1/patients?name=Ana%20Lopez&dob=1984-03-02`);
    await vi.waitFor(
      () => {
        expect(spans.getFinishedSpans().length).toBeGreaterThan(0);
      },
      { timeout: 2000 },
    );
    const [span] = spans.getFinishedSpans();
    const url = String(span?.attributes["url.full"] ?? span?.attributes["http.url"]);
    expect(url).toBe(`${window.location.origin}/api/v1/patients`);
    expect(JSON.stringify(span?.attributes)).not.toContain("Lopez");
  });

  test("errors are recorded by type, without their message", () => {
    recordError(new TypeError("Ada Lovelace was not found"));
    recordError("oops");
    const [a, b] = spans.getFinishedSpans();
    expect(a?.name).toBe("browser.error");
    expect(a?.attributes["error.type"]).toBe("TypeError");
    expect(JSON.stringify(a?.attributes)).not.toContain("Ada");
    expect(b?.attributes["error.type"]).toBe("string");
  });

  test("uncaught errors are recorded", () => {
    window.dispatchEvent(new ErrorEvent("error", { error: new RangeError("x") }));
    expect(spans.getFinishedSpans().map((s) => s.attributes["error.type"])).toContain("RangeError");
    expect(trace.getTracer("t")).toBeDefined();
  });

  test("unhandled rejections are recorded", () => {
    const rejected = Promise.reject(new SyntaxError("x"));
    rejected.catch(() => {}); // the rejection is the test fixture, not a leak
    window.dispatchEvent(
      new PromiseRejectionEvent("unhandledrejection", { reason: new SyntaxError("x"), promise: rejected }),
    );
    expect(spans.getFinishedSpans().map((s) => s.attributes["error.type"])).toContain("SyntaxError");
  });

  test("traceHeaders carries the active span's traceparent, and nothing without one", () => {
    expect(traceHeaders()).toEqual({});
    const tracer = trace.getTracer(SERVICE_NAME);
    const span = tracer.startSpan("test-parent");
    const ctx = trace.setSpan(context.active(), span);
    context.with(ctx, () => {
      const headers = traceHeaders();
      expect(headers.traceparent).toMatch(TRACEPARENT);
      expect(headers.traceparent?.split("-")[1]).toBe(span.spanContext().traceId);
    });
    span.end();
  });

  test("api requests join the active trace", async () => {
    const tracer = trace.getTracer(SERVICE_NAME);
    const span = tracer.startSpan("test-parent");
    const ctx = trace.setSpan(context.active(), span);
    await context.with(ctx, async () => {
      await api.me();
    });
    span.end();
    const header = seen[0]?.get("traceparent");
    expect(header).toMatch(TRACEPARENT);
    expect(header?.split("-")[1]).toBe(span.spanContext().traceId);
    // The existing client header still goes out alongside the traceparent.
    expect(seen[0]?.get("X-Valxel-Client")).toBe("web");
  });
});
