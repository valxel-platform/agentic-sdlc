# Releases and support

What a Valxel release is, what comes with it, and how long each one is supported. It applies to
self-hosted installs (the [install and upgrade guide](self-hosted.md)). The hosted cluster takes
each release first.

## What a release is

A release is one version number for the whole tool, `vMAJOR.MINOR.PATCH`, covering three images
and the chart:

| Part | Image |
|---|---|
| The tool | `ghcr.io/valxel-platform/agentic-sdlc` |
| LLM gateway | `ghcr.io/valxel-shared-services/ss-llm-gateway` |
| Audit sink | `ghcr.io/valxel-shared-services/ss-audit-sink` |
| Helm chart | `deploy/helm/valxel` at the release's tag (its `version` and `appVersion`) |

Each image is built in GitHub Actions, scanned, signed by digest with keyless cosign (GitHub's
OIDC identity), and has an SPDX SBOM and build provenance attached in the registry (the repos'
`publish.yml`). Installs use digests, never tags.

- **Minor** releases (`v0.3.0`) bring features. They may change the database. They come out
  about monthly.
- **Patch** releases (`v0.3.1`) fix bugs and vulnerabilities, with no database changes and no
  new required values. They come out when needed.
- **Major** releases are for changes that need work from the customer beyond the values file
  (a new required cloud resource, a removed value). The release notes give the steps.

## Release notes

Every release has notes on its GitHub release page, with:

- the three image digests and the chart version;
- what changed, for operators and for product teams;
- any new or changed values, and their defaults;
- a **migration note**, if the database changes: what changes, whether a rollback needs the
  snapshot (see [rolling back](self-hosted.md#4-upgrade)), and roughly how long the change takes
  on a large install;
- security fixes, with their CVE or advisory ids.

## Upgrading

- Patch releases can be applied straight away. Skip any number of them within a minor.
- Minor releases are applied in order, one at a time. Each one's database changes assume the
  release before it.
- The tool applies its database changes when it starts. Take a snapshot first (the
  [upgrade steps](self-hosted.md#4-upgrade)).

## Support window

> **Proposed, to confirm before the first self-hosted customer:** the numbers below are a
> starting point, not yet a commitment.

| | |
|---|---|
| Supported releases | Each minor release, for **6 months** after it ships |
| Critical vulnerabilities (CVSS 9 or above, or exploited) in a supported release | A patch within **7 days** of the fix being known |
| High vulnerabilities (CVSS 7 to 8.9) | A patch within **30 days** |
| Other bugs | Fixed in the next minor; patched into supported releases only if they lose data or block work |
| Releases past their window | Not patched; upgrade to a supported release |
| Kubernetes, OpenChoreo, Agent Sandbox, Cilium, Strimzi | The versions each release was tested with are in its notes; a release supports the versions current when it ships and one minor back |

Valxel has no access to a self-hosted install unless the customer grants it. Support works from
what the customer shares: logs, the output of the [checks](self-hosted.md#3-check), and the
values file with its secrets removed.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md): GitHub's private vulnerability reporting on the repository.
Reports are acknowledged within two business days.
