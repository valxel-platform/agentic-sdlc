# Valxel, self-hosted: the Helm chart

This chart installs the Valxel tool on a customer's own Kubernetes, beside their OpenChoreo
control plane. It is the same layout Valxel runs for hosted customers (platform-foundation
`gitops/apps`), with one tenant: the customer's code, secrets and audit records stay in their account.

It installs:

| Part | What it is |
|---|---|
| API | Two replicas behind a Service, routed through Gateway API or an Ingress |
| Workers | Run the queued work and the watcher |
| Provisioning worker | Creates product repos; the only process that can sign for the Provisioning App |
| Agent sessions | A sandbox each (Agent Sandbox) under gVisor, in their own namespace, with a warm pool |
| LLM gateway | Every model call goes through it; it alone holds the model key |
| Audit sink | Keeps the audit records in Postgres, hash-chained, and in an Object Lock bucket |
| Audit Kafka | Optional, with Strimzi; or the customer's own Kafka |

Strict mode is on. If anything it needs is missing, the API refuses to start and says what's missing.

## What the customer provides

- **Kubernetes 1.29 or later:**
  - a gVisor node pool, with a `RuntimeClass` named `gvisor` or the one set in `sessions.runtimeClassName`;
  - Agent Sandbox (`extensions.agents.x-k8s.io`);
  - their OpenChoreo.
- **Cilium for network policy (recommended).** With `networkPolicy.flavor: kubernetes`, sessions can reach any public address on 443, because plain NetworkPolicy can't name hosts.
- **Postgres:** a database each for the tool, the gateway and the audit sink. The tool's login must not be a superuser, so row-level security applies to it.
- **Kafka:** theirs, or Strimzi watching this namespace (`kafka.mode`).
- **Object storage with Object Lock in compliance mode:** the checkpoint archive, the audit anchors and the audit archive.
- **KMS:**
  - the delivery GitHub App's key and the Provisioning App's key, each imported as a sign-only key;
  - roles for the tool's, the provisioning worker's and the sink's service accounts, through Pod Identity or IRSA annotations.
- **A ReadWriteMany storage class** for artifacts (EFS, Filestore, Azure Files...).
- **Two GitHub Apps in their org (delivery and provisioning), and a GitHub OAuth app or Cognito for sign-in.**
- **Their own model key.** For PHI, their own BAA with the provider (`gateway.baa: true`).
- **DNS and TLS** for `publicUrl`.

## Secrets

Nothing secret goes in values. The chart reads these Secrets, which the customer creates or has
External Secrets make (`externalSecrets.items`):

| Secret (value) | Keys |
|---|---|
| `secrets.tool` | `DATABASE_URL`, `VALXEL_SESSION_SECRET`, `VALXEL_GITHUB_WEBHOOK_SECRET`, `VALXEL_LLM_GATEWAY_API_KEY`, `VALXEL_GITHUB_OAUTH_CLIENT_SECRET` or `VALXEL_COGNITO_CLIENT_SECRET`, and any other secret setting the tool reads |
| `secrets.provisioner` | `DATABASE_URL` |
| `gateway.existingSecret` | `ANTHROPIC_API_KEY`, `GATEWAY_DATABASE_URL`, optionally `OPENAI_API_KEY` |
| `auditSink.existingSecret` | `OWNER_URL`, `WRITER_URL`, `WRITER_PASSWORD` |
| `kafka.users.*` (external Kafka only) | `password`; usernames are `valxel-gateway`, `valxel-sink`, `valxel-tool` |

The tool reads its secrets as files, one per name, never from the environment. `gateway.toolKeySha256` is the SHA-256 of `VALXEL_LLM_GATEWAY_API_KEY` (`printf %s "$KEY" | sha256sum`). Only that hash is in the gateway's policy.

## Install

```sh
helm install valxel deploy/helm/valxel -n valxel --create-namespace -f my-values.yaml
```

Every image is set by digest: `image.digest`, `gateway.image.digest` and `auditSink.image.digest`. Each release lists its digests, signatures and SBOMs. `ci/test-values.yaml` shows a complete set of values with made-up contents.

## Checked in CI

`tests/test_helm_chart.py` renders the chart with each Kafka mode, network flavor and route, and checks:

- every process runs as non-root, with a read-only root, no privilege escalation and all capabilities dropped;
- images are set by digest;
- no secret value is written into a manifest;
- the settings are production's;
- only the provisioning worker can sign for the Provisioning App;
- sessions reach only the egress list and the gateway.

It also checks that the chart refuses values production would refuse, and that the sessions' egress list is the tool's own (`config/egress.yaml`).
