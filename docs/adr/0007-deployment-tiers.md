# 0007. Three ways to run the tool, from one image and one set of settings

Date: 2026-11-19
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

The tool runs in production only on the tools EKS cluster (platform-foundation): Flux, Istio
ambient, Cilium, gVisor session sandboxes, the LLM gateway, an audit Kafka and sink, RDS, EFS,
Langfuse. That is the right home at scale, and most of the open rollout work (T-43 to T-60) is
standing it up. Meanwhile:

- nobody can use the tool for real without that cluster: `docker compose up` runs stub agents and
  in-memory delivery, and live delivery expects OpenChoreo;
- early customers and evaluators need something smaller than an EKS cluster to run it;
- the images the cluster pulls (tool, gateway, audit sink) are built in CI but never published.

## Decision

The tool runs in three tiers. Each is built from the same published images and the same
settings (environment variable names, `config/`, governance), and only differs in what surrounds it:

| | Laptop | Single VM | Cluster |
| --- | --- | --- | --- |
| For | building with the tool, end to end, on one machine | early customers and small teams | scale, regulated tenants, cells (T-02) |
| Runs | Docker Compose | Docker Compose, hardened | EKS tools cluster (platform-foundation) |
| Products deploy to | the same Docker (lite runtime, ADR via lite mode) | lite VMs, or OpenChoreo | OpenChoreo data planes |
| Images built by | local Docker from the GitHub repo | the VM's Docker, or OpenChoreo | OpenChoreo build plane |
| Agent sessions | a container per session (gVisor when installed) | a container per session under gVisor | Agent Sandbox pods under gVisor |
| Model calls | the LLM gateway in Compose | the LLM gateway in Compose | the tools-cluster gateway |
| Audit | Kafka and the sink in Compose | the same, archive to S3 Object Lock | Strimzi Kafka, the sink, Object Lock |
| Sign-in | dev sign-in | GitHub or Cognito, TLS | GitHub or Cognito |
| Strict mode | off: stand-ins allowed | on, the VM tier's checks | on, the cluster's checks |

`VALXEL_TIER` (laptop, vm, cluster) says which one an instance is. Strict mode checks what the tier
requires and no more: the laptop allows dev sign-in and local builds, the VM requires TLS, real
sign-in, a session sandbox and backups, the cluster everything it requires today.

Order of work: publish the images (all tiers need them), then the laptop tier (the user asked for
it first, and the VM tier is the laptop hardened), then the VM tier, while the cluster's rollout
continues as tracked.

## Alternatives

- Cluster only: the most isolated, but nobody can use the tool for real until the whole platform
  is up, and it's too much for an early customer.
- A separate "dev edition" codebase or config: diverges from production and hides its problems.
- Laptop on kind or k3d with OpenChoreo: closest to the cluster, but heavy on a laptop and slow to
  start; the lite runtime already gives products a real deployment on Docker.

## Consequences

- One image and one set of settings to test; tier differences are what surrounds the tool.
- Strict mode gains a notion of tier, and each tier's checks need tests.
- The laptop tier mounts the Docker socket into the tool (to build and run products and sessions):
  acceptable on a developer's own machine, never in the VM or cluster tiers, which strict mode enforces.
- Local builds are unsigned and unscanned; the VM and cluster tiers don't accept them.
