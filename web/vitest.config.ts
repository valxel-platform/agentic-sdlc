import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Test-only config: vite.config.ts stays the dev/build config untouched.
// Vitest picks this file up in preference to vite.config.ts.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    // Unit tests only: Playwright specs live in e2e/ and must not be
    // collected by Vitest (their test() is Playwright's, not Vitest's).
    include: ["src/**/*.test.{ts,tsx}"],
    // No globals: tests import { describe, it, expect, vi } from "vitest" explicitly.
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    // JUnit for the CI report step (.github/workflows/ci.yml): the "unit / report" status.
    reporters: ["default", "junit"],
    outputFile: { junit: "reports/junit.xml" },
    coverage: {
      provider: "v8",
      // text for the terminal, json-summary for the CI report step, lcov for diff-cover's
      // coverage of new code.
      reporter: ["text", "json-summary", "lcov"],
      // Report coverage even when tests fail, so the CI "Report results"
      // step can still post counts (report.py tolerates a missing file,
      // but a present summary keeps the coverage number in the status).
      reportOnFailure: true,
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/test/**", "src/**/*.test.{ts,tsx}"],
      // The platform's 90% floor: the run fails below any of these.
      thresholds: { lines: 90, statements: 90, functions: 90, branches: 90 },
    },
  },
});
