# The laptop tier's worker: the tool's published image, plus the Docker CLI and Compose to build
# products' images and run their environments on this machine (lite/local.py). Laptop only: the VM
# and cluster tiers run the published image as it is, with no Docker access.
ARG VALXEL_VERSION=main
FROM docker:28-cli AS docker
FROM ghcr.io/valxel-platform/agentic-sdlc:${VALXEL_VERSION}
USER root
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
USER valxel
