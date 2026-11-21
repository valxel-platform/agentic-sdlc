# Valxel on a laptop

The whole tool on one machine (ADR 0007, the laptop tier): live models, real GitHub pull requests,
and your products built and deployed on this machine's Docker. Nothing runs in the cloud.

## Start

```sh
pip install valxel-sdlc            # or: uv tool install valxel-sdlc
printf '%s\n%s\n' "$ANTHROPIC_KEY" "$GITHUB_TOKEN" | valxel dev init --anthropic-key-stdin --github-token-stdin
valxel dev doctor
valxel dev up                      # http://localhost:8080, signed in with dev sign-in
```

`init` writes `~/.valxel/laptop/.env` (owner-only) and the gateway policy. Run it again to change a key;
the keys you don't pass stay.

The GitHub token needs to create repos and open pull requests in your products' orgs. A fine-grained
token works with Administration, Contents, Pull requests and Workflows (read and write). The delivery
GitHub App works too: set `VALXEL_GITHUB_APP_ID` and `VALXEL_GITHUB_APP_PRIVATE_KEY` in `.env`.

If the images are private, run `docker login ghcr.io` first.

## What runs

| Service | Role |
|---|---|
| api | The tool's API and web app, on 127.0.0.1:8080 |
| worker | Agents, builds and deploys. It is the only service with the Docker socket |
| llm-gateway | The only service with the model key. It applies the data-class rules and the budget, and audits every call |
| kafka, audit-sink | The gateway's audit records, into the `audit` database |
| postgres | The tool's database and the audit database |

Products are built with `docker build` from their repos at the merged commit, and each environment
(development, staging, production) runs as its own Compose project. External endpoints are published
on 127.0.0.1 at a port that stays the same across deploys. `valxel dev urls` lists them.

## Connecting GitHub through the sign-up wizard

A personal token is the quickest start. To test the GitHub step customers go through instead
(Install, GitHub's consent screen, Valxel checking that the installation belongs to whoever
installed it, then the next step), give the laptop its own pair of GitHub Apps:

```sh
valxel dev github-apps --org your-test-org   # registers the Delivery and Provisioning Apps on GitHub
valxel dev up                                # restart, so the tool picks them up
```

Then open http://localhost:8080, sign up (choose a plan, name the workspace), and click
**Install** in the wizard's GitHub step for both Apps. `valxel dev doctor` shows which accounts
each App is installed on.

- **Registration.** `github-apps` uses GitHub's manifest flow: you confirm each App's name on
  GitHub, and its id, OAuth client, private key and webhook secret go into `.env`. They're never
  printed.
- **Where to register.** The Apps are private, so register them under the org you'll install them
  on. Leave out `--org` to register them under your own account.
- **Which credentials win.** Once the Apps are set up, the tool uses them instead of the personal
  token. Repos are created with the Provisioning App, as on the platform.
- **Webhooks.** The Apps' webhooks are registered off, because GitHub can't reach a laptop. CI and
  pull request changes are picked up on the watcher's timer instead.
- **Ports.** Both laptop ports (8080, and 8090 beside a local OpenChoreo) are registered as callback
  URLs, so switching modes doesn't need new Apps.

## With OpenChoreo instead of plain Docker

To test the cluster tier's path on your machine (components, workflow runs, releases and release
bindings through OpenChoreo's API), run OpenChoreo itself in a local k3d cluster:

```sh
valxel dev cluster up      # OpenChoreo 1.3.3 (the platform's version) with its build plane; needs k3d, kubectl, helm
valxel dev up              # the tool, now on http://localhost:8090 (OpenChoreo's gateway takes 8080)
valxel dev cluster check   # what the tool calls and names that this OpenChoreo doesn't have
```

`cluster up` does the following:

- It runs OpenChoreo's own k3d installer at the pinned release. The script's hash is checked first,
  and it isn't run if the hash changed.
- It stores your GitHub token in the cluster's OpenBao, so builds can clone private repos. The token
  is passed on stdin, never as an argument.
- It adds two names the tool's repo standards use. `docker` is OpenChoreo's `dockerfile-builder`
  workflow under another name, and `web-app` is its `web-application` component type. These names
  exist only on the laptop: the platform's own `docker` workflow signs images with a cloud KMS key,
  which a laptop doesn't have.
- It switches `.env` to `VALXEL_LOCAL_RUNTIME=openchoreo`.

The OpenChoreo console is at http://openchoreo.localhost:8080 (admin@openchoreo.dev / Admin@123).
The tool signs in with the installer's development client, whose credentials are public. That's
fine for a cluster that only this machine can reach.

`valxel dev cluster down` deletes the cluster and switches back to plain Docker.

## Limits

- Not for anything shared. Dev sign-in lets anyone who reaches port 8080 in, and the worker controls
  this machine's Docker.
- Synthetic data only, never real PHI. The gateway's provider has no BAA here, so PHI is refused.
- Local images aren't scanned or signed. Strict mode, used on the VM and cluster tiers, refuses
  `VALXEL_DELIVERY=local`.
- Agent sessions each run in a container of their own, started by the session launcher, on an
  internal network that reaches only the LLM gateway and the egress proxy (GitHub and package
  registries; add hosts with `VALXEL_EGRESS_ALLOW`). With gVisor installed in Docker (`runsc`)
  they run under it; without it, under Docker's default runtime, which isolates less.

`valxel dev down` stops everything. `valxel dev down --wipe` also deletes the databases and artifacts.
