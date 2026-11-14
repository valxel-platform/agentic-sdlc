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

## Where secrets come from

`VALXEL_SECRET_STORE` chooses the store (`valxel_sdlc.secret_ring`):

| Store | Reads | For |
| --- | --- | --- |
| `aws` | AWS Secrets Manager: `<prefix>/platform/<NAME>`, `<prefix>/products/<product>/<NAME>` for a product's own secrets, `<prefix>/orgs/<org>/<NAME>`; current and previous are `AWSCURRENT` and `AWSPREVIOUS`. `VALXEL_SECRET_PREFIX`, default `valxel`. Cached for a minute. | Valxel Cloud: scope each tenant's access by path |
| `files` | `VALXEL_SECRETS_DIR/<NAME>` and `<NAME>.previous` | Any other cloud or Vault, through the Secrets Store CSI driver, External Secrets or Vault Agent |
| `env` | `<NAME>` and `<NAME>_PREVIOUS` | Laptops and tests |

With `aws` or `files`, strict mode refuses to start while a secret is still in the environment,
and in live mode without a store it warns. The process reaches AWS with its own role, never a
stored key.

The GitHub App's key can stay in AWS KMS: import it as a sign-only key and set
`VALXEL_GITHUB_APP_KMS_KEY_ID`; the tool asks KMS to sign each token and never holds the key.
Otherwise it reads `VALXEL_GITHUB_APP_PRIVATE_KEY` from the store, or the file
`VALXEL_GITHUB_APP_PRIVATE_KEY_FILE` (`valxel_sdlc.signing`).

## Repo administration: two GitHub Apps

Creating repos and applying their settings (rulesets, security features, team access, custom
properties) needs `administration` rights. A GitHub App's permissions belong to the App, so any
process that can sign for it can mint an admin token. The tool therefore uses two Apps
(`valxel_sdlc.provision_queue`):

| App | Permissions | Who signs for it |
| --- | --- | --- |
| Delivery (`VALXEL_GITHUB_APP_*`) | contents, pull requests, statuses, checks, alerts; no administration | The API process |
| Provisioning (`VALXEL_GITHUB_PROVISIONER_APP_*`) | administration, contents, pull requests, security settings, custom properties | Only `valxel provisioner`, a separate worker with its own AWS role |

With `VALXEL_PROVISIONING=queue`, the API process puts provisioning requests on a queue and the
pipeline waits; the worker creates the repos. The worker trusts governance, not the queue: a repo
must be the plan G4 approved for that feature, recomputed from governance, and a governance repo
must be the one its inception's product gate approved. A refused request comes back to the feature
as a provisioning escalation.

Keep each App's key in KMS, with a key policy that lets only its own process's role sign. Strict
mode refuses to start an API process that creates repos itself or can reach the Provisioning App's
key.

## Rotating secrets

The secrets the tool checks or signs with are key rings (`valxel_sdlc.secret_ring`): a current
value and, during a rotation, the previous one. Checks accept either; signing uses the current.
Each is read from the store above; changes are picked up without a restart. In AWS Secrets Manager,
its own rotation keeps the old value as `AWSPREVIOUS`, so both stay valid until the next rotation.

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
