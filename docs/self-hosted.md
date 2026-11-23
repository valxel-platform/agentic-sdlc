# Self-hosted Valxel: install and upgrade

This guide is for a customer's platform team that runs the Valxel tool in its own cloud account,
beside its own OpenChoreo. It covers the cloud resources to prepare, the install, the checks
afterwards, upgrades and backups.

Related pages:
- [the chart's README](../deploy/helm/valxel/README.md): what the chart installs, and every value;
- [the security packet](security-packet.md): what security reviewers ask for;
- [releases and support](releases.md): versions and how long each is supported.

Plan on about a day for a first install, most of it spent on step 1. The examples use AWS; on another cloud, use its equivalent for each resource.

## 1. Prepare

### Kubernetes

- Kubernetes 1.29 or later, running the customer's OpenChoreo control plane.
- A gVisor node pool and a `RuntimeClass` for it (default name `gvisor`). Each agent session
  runs in a pod on that pool.
- [Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox) installed, which provides
  the `extensions.agents.x-k8s.io` resources.
- Cilium as the network policy engine (recommended). It's the only option that limits sessions
  to named hosts (`networkPolicy.flavor: cilium`).
- A Gateway API gateway or an ingress controller, with a TLS certificate for the tool's domain.
- External Secrets, if the Secrets in step 1.6 come from a secret store.
- Strimzi watching the install's namespace, unless the customer brings their own Kafka.

### Databases

Postgres 15 or later. Encrypt at rest, and keep automated backups with point-in-time recovery. Create three databases, each with its own login:

```sql
-- The tool. Not a superuser and no BYPASSRLS: workspaces are separated by row-level
-- security, and strict mode refuses a login that would skip it.
CREATE ROLE valxel LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD '...';
CREATE DATABASE valxel OWNER valxel;

-- The LLM gateway: spend, reservations, session tokens.
CREATE ROLE llm_gateway LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB PASSWORD '...';
CREATE DATABASE llm_gateway OWNER llm_gateway;

-- The audit sink. The owner sets up the tables and triggers and creates audit_writer (so it
-- needs CREATEROLE); the sink itself runs as audit_writer, which may only add rows.
CREATE ROLE audit_owner LOGIN NOSUPERUSER CREATEROLE NOCREATEDB PASSWORD '...';
CREATE DATABASE valxel_audit OWNER audit_owner;
```

The tables are created when each part first starts. Nothing else needs running against the
databases by hand.

### Object storage

Create three buckets, with versioning on and Object Lock in **compliance** mode. Encrypt them with the customer's KMS key.

| Bucket | Value | Default retention |
|---|---|---|
| Checkpoint archive (history of regulated products) | `archives.checkpointBucket` | 2190 days |
| Audit anchors (hash-chain heads) | `archives.auditAnchorBucket` | 2190 days |
| Audit archive (the sink's locked copy) | `archives.auditArchiveBucket` | 2190 days, matching `archives.auditArchiveRetentionDays` |

In compliance mode, no one can delete or shorten a locked object before its retention ends, the
account root included. Set the retention deliberately.

### Identities and KMS

Create three cloud roles, bound to the chart's service accounts (EKS Pod Identity, or IRSA through `tool.serviceAccountAnnotations` and its siblings):

| Service account | Needs |
|---|---|
| `<release>` (API and workers) | `kms:Sign` and `kms:GetPublicKey` on the delivery App's key; `s3:PutObject` on the checkpoint archive and anchor buckets; Cost Explorer read, if costs are shown |
| `<release>-provisioner` | `kms:Sign` and `kms:GetPublicKey` on the Provisioning App's key, **and no other role may have them** |
| `<release>-audit-sink` | `s3:PutObject` and `s3:PutObjectRetention` on the audit archive bucket |

Each GitHub App's private key goes into KMS as a sign-only key (`RSA_2048`, `SIGN_VERIFY`, origin
`EXTERNAL`), so no process ever holds the key itself:

```sh
umask 077; cd "$(mktemp -d)"
openssl pkcs8 -topk8 -nocrypt -inform PEM -in app.pem -outform DER -out key.der
aws kms get-parameters-for-import --key-id "$KEY" \
  --wrapping-algorithm RSA_AES_KEY_WRAP_SHA_256 --wrapping-key-spec RSA_4096 > params.json
jq -r .PublicKey params.json | base64 -d > wrapping.der
jq -r .ImportToken params.json | base64 -d > token.bin
openssl rand -out aes.bin 32
openssl enc -id-aes256-wrap-pad -K "$(xxd -p -c 64 aes.bin)" -iv A65959A6 -in key.der -out material.bin
openssl pkeyutl -encrypt -in aes.bin -out aes.wrapped -inkey wrapping.der -keyform DER -pubin \
  -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256
cat aes.wrapped material.bin > wrapped.bin
aws kms import-key-material --key-id "$KEY" --encrypted-key-material fileb://wrapped.bin \
  --import-token fileb://token.bin --expiration-model KEY_MATERIAL_DOES_NOT_EXPIRE
# The two hashes must match:
aws kms get-public-key --key-id "$KEY" --query PublicKey --output text | base64 -d | sha256sum
openssl pkey -in app.pem -pubout -outform DER | sha256sum
```

Then shred the working directory, and delete the PEM from GitHub once the tool is up.

### GitHub Apps

Register two GitHub Apps in the customer's organization. Both are private to that organization.

| App | Permissions | Webhook |
|---|---|---|
| Delivery | Contents, pull requests and commit statuses: write. Checks, metadata, code scanning alerts, secret scanning alerts and Dependabot alerts: read. Organization members: read, so gates re-check team membership when someone decides | `https://<domain>/api/webhooks/github`, events: pull request, check suite, check run, status, push. Set a webhook secret |
| Provisioning | Administration, contents, pull requests, workflows, code scanning alerts, secret scanning alerts, Dependabot alerts and repository custom properties: write. Organization custom properties: admin. Members and metadata: read | None |

For both Apps, the callback URL is `https://<domain>/api/connections/github/callback`. Note each App's id, slug and client id, and generate a client secret.

Sign-in uses a GitHub OAuth app (callback `https://<domain>/api/auth/callback`), a Cognito user
pool, or the team's own OpenID Connect provider: Okta, Entra ID, Google Workspace, Keycloak and
others (`auth.mode: oidc`, redirect URI `https://<domain>/api/auth/oidc/callback`; the
[operators guide](operators.md#sign-in-and-who-can-decide) has the details, including groups as
approvers).

### Secrets

The chart reads these Secrets in its namespace. Make them by hand, or have External Secrets make them (`externalSecrets.items`).

| Secret | Keys |
|---|---|
| `valxel-tool` | `DATABASE_URL` (the `valxel` login, `sslmode=require`); `VALXEL_SESSION_SECRET` (`openssl rand -hex 32`); `VALXEL_GITHUB_WEBHOOK_SECRET`; `VALXEL_LLM_GATEWAY_API_KEY` (`openssl rand -hex 32`); `VALXEL_GIT_BROKER_KEY` (`openssl rand -hex 32`: seals agent sessions' git passes; the egress proxy mounts it alone); `VALXEL_GITHUB_OAUTH_CLIENT_SECRET`, `VALXEL_COGNITO_CLIENT_SECRET` or `VALXEL_OIDC_CLIENT_SECRET`; `VALXEL_GITHUB_APP_CLIENT_SECRET`; `VALXEL_GITHUB_PROVISIONER_APP_CLIENT_SECRET` |
| `valxel-provisioner` | `DATABASE_URL` |
| `valxel-llm-gateway` | `ANTHROPIC_API_KEY` (the customer's own key; for PHI, under their own BAA); `GATEWAY_DATABASE_URL`; optionally `OPENAI_API_KEY` |
| `valxel-audit-database` | `OWNER_URL` (`audit_owner`); `WRITER_URL` (`audit_writer`, with a new password); `WRITER_PASSWORD` (that password) |

Put the SHA-256 of `VALXEL_LLM_GATEWAY_API_KEY` in `gateway.toolKeySha256`. Only the hash is stored in the gateway's policy:

```sh
printf %s "$VALXEL_LLM_GATEWAY_API_KEY" | sha256sum | cut -d' ' -f1
```

## 2. Install

Find the release's image digests in its release notes (see [releases](releases.md)). Check their signatures before installing:

```sh
for image in ghcr.io/valxel-platform/agentic-sdlc@sha256:... \
             ghcr.io/valxel-shared-services/ss-llm-gateway@sha256:... \
             ghcr.io/valxel-shared-services/ss-audit-sink@sha256:...; do
  repo=$(echo "$image" | cut -d/ -f2-3 | cut -d@ -f1)
  cosign verify "$image" \
    --certificate-identity-regexp "^https://github.com/$repo/" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
done
```

Write the values file. [`ci/test-values.yaml`](../deploy/helm/valxel/ci/test-values.yaml) lists every value an install needs. Then:

```sh
helm install valxel deploy/helm/valxel -n valxel --create-namespace -f values.yaml
```

The chart refuses to render if a required value is missing, an image isn't set by digest, or
`publicUrl` isn't https.

## 3. Check

1. **Everything is running:**
   - `kubectl -n valxel get pods`: everything is Running and Ready.
   - In the sessions namespace, the warm pool's sandboxes are Ready.
2. **Strict mode passed:** the API's log shows no `strict mode` refusal. If the API keeps
   restarting, its log names each missing piece:
   `kubectl -n valxel logs deploy/valxel-api | grep -A20 strict`.
3. **The audit chains are intact:** run
   `kubectl -n valxel exec deploy/valxel-api -- valxel audit` and
   `kubectl -n valxel exec deploy/valxel-audit-sink -- audit-sink verify`. Both exit 0.
4. **Sign-in works:** open the tool's URL, sign in, create a workspace, and connect GitHub with
   both Apps.
5. **A session runs end to end:** create a small feature in a test product. Take it through spec
   and design, then watch one coding session.
   - The session runs in the sessions namespace.
   - Its model calls appear in the gateway's log.
   - A pull request opens.
6. **Session egress is limited:** check the sessions' network policy from a test sandbox. A
   request to an address that isn't on the egress list is refused.

## 4. Upgrade

Upgrade one release at a time, in order: no skipping versions. Before upgrading:

1. Read the release notes, especially any **migration note**.
2. Check the new digests' signatures, as at install.
3. Take a database snapshot of all three databases. If the release has a migration note, also
   copy the artifacts volume.

Then:

```sh
helm upgrade valxel deploy/helm/valxel -n valxel -f values.yaml \
  --set image.digest=sha256:... --set gateway.image.digest=sha256:... --set auditSink.image.digest=sha256:...
```

The API and workers roll one pod at a time. A worker that is stopped finishes its runs for 25 seconds, then hands the rest back to the queue. The tool applies any database change when it starts.

Afterwards, run checks 1 to 3 from section 3.

**Rolling back** (`helm rollback valxel`) is safe across releases with no migration note. Across
one with a migration note, the new schema stays after the rollback. In that case, restore the
snapshot first, then roll back. Work done in between is lost, so roll forward with a fix when
you can.

Upgrade Kubernetes, OpenChoreo, Cilium, Strimzi and Agent Sandbox separately from the tool,
never at the same time.

## 5. Back up and restore

| What | How | Restore |
|---|---|---|
| The three databases | The cloud's automated backups with point-in-time recovery, kept at least 14 days | Restore to a new instance, then point the Secrets at it |
| Artifacts (documents, attachments) | Snapshots of the ReadWriteMany volume (e.g. AWS Backup for EFS), daily | Restore the volume, then mount it again (`artifacts.existingClaim`) |
| Audit archive, anchors, regulated checkpoints | Object Lock: nothing to back up; replicate the buckets to a second region if needed | Read in place |
| Settings | The values file and the Secrets' source, kept in the customer's own git and secret store | `helm upgrade --install` |

Test a restore at least every quarter: restore the databases and artifacts into a second
namespace, then run section 3's checks against it.

## 6. Uninstall

`helm uninstall valxel -n valxel` removes the tool. Some things stay on purpose: the artifacts
volume (the chart marks it to keep), the databases and the buckets. Products' repos and
environments are the customer's and aren't touched.
