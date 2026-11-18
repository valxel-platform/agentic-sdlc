import { expect, test } from "@playwright/test";

/**
 * Security headers.
 *
 * Checked 2026-11-18 with `curl -I` against vite preview on
 * http://localhost:4173 (the server this suite runs against): it sends
 * Content-Type, Cache-Control: no-cache and an ETag — and NO
 * Content-Security-Policy and NO Strict-Transport-Security. The FastAPI
 * app that serves web/dist in production has no security-headers
 * middleware either (the only CSP in the backend is a sandboxed
 * `default-src 'none'` on one attachment-preview route), so CSP/HSTS
 * cannot honestly pass against any server runnable locally. They are a
 * deployment-layer concern — the reverse proxy in front of the deployed
 * app — and the skipped test below is the one to enable once that layer
 * is known, in post-deploy against BASE_URL.
 */

test("vite preview serves the app as HTML with no-cache", async ({ request }) => {
  const res = await request.get("/");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toContain("text/html");
  expect(res.headers()["cache-control"]).toContain("no-cache");
});

test.skip("CSP and HSTS (deployment layer — see the comment above)", async ({ request }) => {
  const res = await request.get("/");
  expect(res.headers()["content-security-policy"]).toContain("default-src 'self'");
  expect(res.headers()["strict-transport-security"]).toContain("max-age=");
});
