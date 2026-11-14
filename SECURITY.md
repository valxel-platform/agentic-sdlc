# Security policy

## Reporting a vulnerability

Do not open a public issue. Use **Security > Report a vulnerability** on this repository
(GitHub private vulnerability reporting). Include what you found, how to reproduce it and the
impact you expect. We acknowledge reports within two business days.

## What this tool holds

The tool runs every product's SDLC, so it holds the platform's most sensitive credentials:
the GitHub App's private key (administration rights in every org), the OpenChoreo service
client, the tracker and diagram tokens, Flipt tokens and the LLM keys. None of them are in this
repository; they come from the environment or a secret store, and the GitHub App mints
short-lived, per-org tokens narrowed to each task.

- No agent holds production credentials or promotes to production (VX-3); people do, through
  the gates.
- Feature descriptions, artifacts and agent prompts never carry patient or cardholder data;
  the gateway refuses PHI to providers without a BAA (VX-7).
- Webhooks are authenticated (GitHub HMAC, Sentry HMAC, bearer tokens for Alertmanager and
  tickets); sign-in uses GitHub OAuth with signed session cookies.

## What this repository does by default

Pull requests only, reviewed by the platform owners (`CODEOWNERS`), with `test`, `web` and
CodeQL required; secret scanning with push protection; Dependabot alerts and security updates;
and a daily check that every managed repo is on the standards (`repo-standards` workflow).

## Rotating secrets

The secrets the tool checks or signs with are key rings (`valxel_sdlc.secret_ring`): a current
value and, during a rotation, the previous one. Checks accept either; signing uses the current.
Each is read from `VALXEL_SECRETS_DIR/<NAME>` and `<NAME>.previous` when that folder has it (as a
secret manager mounts them; changes are picked up without a restart), otherwise from `<NAME>` and
`<NAME>_PREVIOUS` in the environment.

| Secret | Rotate by |
| --- | --- |
| `VALXEL_SESSION_SECRET` | New value as current, old as previous; drop previous after 12 hours (the session lifetime) |
| `VALXEL_GITHUB_WEBHOOK_SECRET` | New as current, old as previous; change the App's webhook secret; drop previous |
| `VALXEL_SENTRY_CLIENT_SECRET` | The same, with Sentry's integration |
| `VALXEL_SIGNALS_TOKEN` | The same; update Alertmanager, ticket, SNS and GlitchTip senders (`valxel glitchtip-token <product>`) |

Strict mode (`VALXEL_STRICT=1`) refuses to start with a published example value, a session secret
shorter than 32 characters or with too few distinct characters, webhook secrets shorter than 20,
or a default database password. In GitHub sign-in mode the server refuses a weak session secret
even without strict mode.
