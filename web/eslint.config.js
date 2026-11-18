import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "reports", "pacts", "playwright-report", "test-results"] },
  {
    // NOTE: template-web uses strictTypeChecked, but this codebase was
    // written without linting and has ~850 pre-existing strictTypeChecked
    // violations (mostly no-confusing-void-expression, no-unnecessary-condition,
    // restrict-template-expressions). Adopting the type-checked tier is a
    // separate cleanup; the gate below enforces the recommended tier plus
    // the react-hooks / react-refresh rules, and it must stay green.
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser },
    },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // This codebase resets local state from props in effects all over
      // (e.g. "when a different decision opens, clear the form"). That
      // pattern predates this config; migrating it is a separate cleanup.
      // Keep the rule off rather than sprinkling disables across 12 sites.
      "react-hooks/set-state-in-effect": "off",
      // Files here colocate small helpers with their component on purpose
      // (e.g. a size() formatter next to the list that renders it). Fast
      // refresh still works for the component; only the helper export is
      // flagged, so the rule is off.
      "react-refresh/only-export-components": "off",
    },
  },
);
