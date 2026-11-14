"""Lite mode: a product's environments as Docker Compose projects on one or two VMs.

A product with `runtime.mode: lite` deploys through `ComposeRuntime`, a second implementation of
the `OpenChoreo` interface (integrations/openchoreo.py), so the gates, agents, change log, audit
trail and incident handling don't change. See the design doc "Valxel lite mode: design and effort".

- `workload`: an OpenChoreo workload.yaml translated into a Compose service.
- `docker`: the Docker CLI on a VM (`DOCKER_HOST=ssh://...`), and an in-memory stand-in for tests.
- `state`: what the runtime remembers: components, releases, bindings, environments.
- `runtime`: `ComposeRuntime`.
"""
