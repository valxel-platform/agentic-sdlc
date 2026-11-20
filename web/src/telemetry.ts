/**
 * Browser tracing with OpenTelemetry.
 *
 * Ported from valxel-platform/template-web's src/telemetry.ts, adapted to this
 * app (no component.ts here, so the service name lives in this file).
 *
 * Each call the app makes to its API carries a W3C `traceparent` header, so the
 * request's trace starts in the browser and continues through the service,
 * agents and MCP servers. Spans carry URL paths, methods, status codes and
 * timings; query strings are removed before a span leaves the browser. Keep
 * personal data out of paths (use ids, not names or dates of birth).
 * Uncaught errors are recorded as `browser.error` spans with the error's type
 * only.
 *
 * EXPORTER ROUTE. The API now takes spans itself at `/otlp/v1/traces` and
 * forwards them to the trace store (VALXEL_OTEL_ENDPOINT), for signed-in
 * people; `/api/me` says `tracing: true` when it does, and main.tsx then
 * starts tracing with that same-origin endpoint. That works in the published
 * image, which has no build-time endpoint. The build-time variable below
 * still works, and wins, for a collector reached directly.
 *
 * The original note, kept for the build-time path:
 * The template points its exporter at `${origin}/otlp/v1/traces` because an
 * nginx in front of the template forwards `/otlp/` to the data plane's
 * collector. This app is served by FastAPI (StaticFiles + an SPA catch-all),
 * so a relative `/otlp/v1/traces` would hit the SPA fallback and return
 * index.html — spans would go nowhere. There is also no collector deployed
 * for this app yet. So the exporter points directly at a collector instead:
 *
 *   VITE_OTEL_EXPORTER_OTLP_ENDPOINT — full OTLP/HTTP traces URL, e.g.
 *   `https://otel.example.com/v1/traces`. (Vite only exposes `VITE_`-prefixed
 *   variables to the browser, hence the prefix; it mirrors the standard
 *   `OTEL_EXPORTER_OTLP_ENDPOINT` name.)
 *
 *   Default: unset, and tracing is DISABLED — startTelemetry() returns null
 *   and nothing is registered, fetched or sent. Set the variable to turn
 *   tracing on. `VITE_OTEL_ENABLED=false` disables it even when an endpoint
 *   is set. No backend `/otlp/` proxy route was added: OTLPTraceExporter
 *   takes an arbitrary `url`, so the browser can reach the collector
 *   directly and no FastAPI change is needed.
 *
 * Tracing always fails open: a missing or unreachable endpoint, or any
 * error while starting, never breaks app boot — startTelemetry() catches
 * startup errors and returns null.
 */
import { context, propagation, SpanStatusCode, trace, type Span } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, type SpanExporter, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { WebTracerProvider } from "@opentelemetry/sdk-trace-web";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

/** This component's name, as it appears in traces and error reports. */
export const SERVICE_NAME = "valxel-sdlc-web";

export interface TelemetryOptions {
  service?: string;
  /**
   * Full OTLP/HTTP traces URL, e.g. `https://otel.example.com/v1/traces`.
   * Defaults to the `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` build-time variable;
   * unset means tracing stays off. Overridden per-call in tests.
   */
  endpoint?: string | null;
  /**
   * Set false to disable tracing entirely. Defaults to the
   * `VITE_OTEL_ENABLED` variable (on unless it is exactly `"false"`).
   */
  enabled?: boolean;
  /** Replaces the OTLP exporter (tests pass an in-memory one). */
  processor?: SpanProcessor;
}

/**
 * Starts browser tracing. Returns the provider, or null when tracing is
 * disabled (no endpoint configured, or explicitly switched off) or fails to
 * start. Never throws.
 */
export function startTelemetry({
  service = SERVICE_NAME,
  endpoint,
  enabled,
  processor,
}: TelemetryOptions = {}): WebTracerProvider | null {
  try {
    const on = enabled ?? import.meta.env.VITE_OTEL_ENABLED !== "false";
    const url = endpoint ?? import.meta.env.VITE_OTEL_EXPORTER_OTLP_ENDPOINT;
    // No collector configured (and no test processor): stay off entirely.
    if (!on || (!url && !processor)) return null;

    const exporter: SpanExporter | undefined = url ? new OTLPTraceExporter({ url }) : undefined;
    const provider = new WebTracerProvider({
      resource: resourceFromAttributes({ [ATTR_SERVICE_NAME]: service }),
      spanProcessors: [processor ?? new BatchSpanProcessor(exporter as SpanExporter)],
    });
    provider.register();
    registerInstrumentations({
      tracerProvider: provider,
      instrumentations: [
        new FetchInstrumentation({
          // Same-origin API calls get traceparent; the exporter's own calls
          // aren't traced (otherwise exporting would trace itself forever).
          ignoreUrls: [
            /\/otlp\//,
            /\/ofrep\//,
            // A collector elsewhere: none of its calls. The API's own /otlp/ (same
            // origin) is already ignored above; ignoring its whole origin would
            // drop traceparent from every API call.
            ...(url && !sameOrigin(url)
              ? [endpointIgnorePattern(url)].filter((p): p is RegExp => p !== undefined)
              : []),
          ],
          propagateTraceHeaderCorsUrls: [],
          // Query strings never reach a trace: they can hold what a person searched for.
          applyCustomAttributesOnSpan: (span) => {
            withoutQueryStrings(span);
          },
        }),
      ],
    });
    window.addEventListener("error", (e) => {
      recordError(e.error);
    });
    window.addEventListener("unhandledrejection", (e) => {
      recordError(e.reason);
    });
    return provider;
  } catch {
    // Tracing must never break app boot.
    return null;
  }
}

/**
 * Headers carrying the active trace's W3C `traceparent` (and `tracestate`,
 * when present). Empty when tracing is off or no span is active, so callers
 * can merge the result into their own headers unconditionally. Used by
 * api.ts so every API request joins the browser's trace.
 */
export function traceHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  propagation.inject(context.active(), headers);
  return headers;
}

export function recordError(error: unknown): void {
  const span: Span = trace.getTracer(SERVICE_NAME).startSpan("browser.error");
  // The type only: messages can echo what a person typed.
  span.setAttribute("error.type", error instanceof Error ? error.name : typeof error);
  span.setStatus({ code: SpanStatusCode.ERROR });
  span.end();
}

/** Whether the endpoint is this app's own origin (the API's /otlp/ route). */
export function sameOrigin(endpoint: string): boolean {
  try {
    return new URL(endpoint).origin === window.location.origin;
  } catch {
    return false;
  }
}

/** A regex matching fetches to the given endpoint's origin, or undefined if the URL is invalid. */
export function endpointIgnorePattern(endpoint: string): RegExp | undefined {
  try {
    const origin = new URL(endpoint).origin;
    return new RegExp(origin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  } catch {
    return undefined;
  }
}

/** URL attributes the fetch instrumentation sets (older and current semantic conventions). */
const URL_ATTRIBUTES = ["http.url", "url.full"];

/** The URL without its query string and fragment. */
export function stripQuery(url: string): string {
  const cut = url.search(/[?#]/);
  return cut < 0 ? url : url.slice(0, cut);
}

export function withoutQueryStrings(span: Span): void {
  const attributes = (span as unknown as { attributes?: Record<string, unknown> }).attributes ?? {};
  for (const name of URL_ATTRIBUTES) {
    const value = attributes[name];
    if (typeof value === "string") span.setAttribute(name, stripQuery(value));
  }
}
