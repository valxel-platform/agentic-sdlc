# Valxel on one VM

The single-VM tier (ADR 0007): the tool for an early customer or a small team, hardened, on one
Linux VM with Docker. Products deploy to their own lite VMs or OpenChoreo, never to this one.

## What you need

- A Linux VM (4 vCPU, 16 GB, 100 GB disk to start) with Docker Engine, the Compose plugin and
  [gVisor](https://gvisor.dev/docs/user_guide/install/) registered as Docker's `runsc` runtime.
- A domain whose DNS points at the VM, and ports 80 and 443 open to it (Let's Encrypt).
- Two GitHub Apps (Delivery and Provisioning) and a GitHub OAuth App for sign-in.
- A model key for the LLM gateway.
- `cosign`, to check the tool's image before an upgrade.

## Set up

```sh
sudo valxel vm init --domain valxel.example.com --email ops@example.com
sudo valxel vm doctor        # names everything still missing
```

`init` writes the bundle to `/opt/valxel` and generates the secrets the tool makes itself:
database passwords, the session secret, the webhook secret, the gateway key and the session
launcher's token. Fill in the rest where `doctor` says:

- `secrets/VALXEL_GITHUB_APP_PRIVATE_KEY`, `secrets/VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY`:
  the Apps' private keys.
- `secrets/VALXEL_GITHUB_OAUTH_CLIENT_SECRET`: the OAuth App's secret.
- `secrets/gateway.env`: `ANTHROPIC_API_KEY=...`. Only the gateway reads it.
- `.env`: the App ids, the OAuth client id, and each image by digest (`...@sha256:...`).

Then `sudo valxel vm up`. The tool is at `https://<your domain>`.

## How it's hardened

- **TLS and sign-in.** Caddy terminates TLS for your domain and is the only thing published.
  Sign-in is GitHub (or Cognito); dev sign-in is refused.
- **Secrets are files** under `/run/secrets` in each container, never environment variables
  (`VALXEL_SECRET_STORE=files`). Each container gets only its own. Only the provisioner can sign
  for the Provisioning App.
- **Agent sessions.** Each one runs in a container of its own under gVisor, started by the session
  launcher, the only process with the Docker socket. Sessions sit on an internal network: they reach
  the LLM gateway, and GitHub and the package registries through the egress proxy, nothing else.
  Allow more hosts with `VALXEL_EGRESS_ALLOW`.
- **The database.** The tool's login owns its database but isn't a superuser and can't bypass
  row-level security, which is on (`VALXEL_TENANCY=rls`).
- **Containers.** The tool's containers run read-only, as a non-root user, with every capability
  dropped and no new privileges.
- **Strict mode** is on with the VM tier's checks: the tool won't start with dev sign-in, without
  TLS, without gVisor, with the Docker socket mounted into it, or without backups set up.

## Backups

Every night the `backup` service dumps both databases and archives the artifacts into the
`backups` volume, keeping `VALXEL_BACKUP_KEEP_DAYS` (14) days. The tool checks the last one daily
and alerts after 26 hours without a successful backup. `valxel vm backup` runs one now.

Copy them off the VM as well, for example with restic or `rsync` from
`/var/lib/docker/volumes/valxel_backups/_data`. A backup that lives only on the VM is lost with it.

To restore a night: `sudo valxel vm restore 20261121T023000Z`. The tool stops while it restores.

## Upgrades

```sh
sudo valxel vm upgrade ghcr.io/valxel-platform/agentic-sdlc@sha256:<digest>
```

The upgrade does four things in order:

1. Checks the image's signature from the tool's release workflow.
2. Takes a backup.
3. Moves the tool's containers to the new digest.
4. Waits until it's healthy. If it isn't within five minutes, it goes back to the previous digest.

Pin the other images by digest in `.env` the same way.
