# syntax=docker/dockerfile:1.7
# Stage 1: build the web interface (React + Vite) into web/dist.
FROM node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

# Stage 2: the plugins whose skills the agents follow: Anthropic's Product Management, Engineering
# and Design plugins (Apache 2.0) at a pinned commit, and ours (plugins/vrixel). The pin is config
# (config/plugins.yaml), so moving it goes through the config repo's evals; this default must name
# the same commit (tests/test_plugins.py checks), and CI builds with the config's. CI also checks
# that every skill the phases name is in them.
FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS plugins
ARG KNOWLEDGE_WORK_PLUGINS_REF=e605d8ddbfb688da57dfaaceebd93f12a64ef1d3
ADD --keep-git-dir=false https://github.com/anthropics/knowledge-work-plugins.git#${KNOWLEDGE_WORK_PLUGINS_REF} /upstream
COPY plugins /src/plugins
# Skills and commands only: the plugins' connectors (.mcp.json) and hooks are left out. REF records
# the commit, so the tool can tell when a config pins another one.
RUN /src/plugins/prepare.sh /upstream /src/plugins/vrixel /plugins \
 && echo "${KNOWLEDGE_WORK_PLUGINS_REF}" > /plugins/REF

# Stage 3: OpenAI's Codex CLI, for roles that run on the codex runtime (models.yaml).
FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS codex
ARG CODEX_VERSION=0.167.0
# The download must match this checksum (moving the version moves both, by pull request).
ADD --checksum=sha256:9d64836106d5b374e90ee4454c7ebcde7ca7106558bd7e41a34b9f26ebda251a \
    https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz /tmp/codex.tar.gz
RUN tar -xzf /tmp/codex.tar.gz -C /tmp && install -m 0755 /tmp/codex-x86_64-unknown-linux-musl /codex

# Stage 4: the API, which also serves the built web interface.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VRIXEL_ROOT=/app \
    VRIXEL_ARTIFACT_DIR=/data/artifacts \
    VRIXEL_SKILLS_DIR=/opt/vrixel/plugins \
    VRIXEL_PLUGIN_SRC=/opt/vrixel/plugin-src \
    VRIXEL_CODEX_HOME=/opt/vrixel/codex-home

WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
# Exactly the locked versions, each checked against its hash (requirements.lock is uv.lock's export;
# CI fails when they differ), then the tool itself with no further resolution.
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY config ./config
# Sample governance repos, used only when VRIXEL_GOVERNANCE_SOURCE=local.
COPY examples ./examples
COPY --from=web /web/dist ./web/dist
COPY --from=plugins /plugins /opt/vrixel/plugins
# What lays out a copy at another pin (an eval run's or the canary's config): vrixel_sdlc.plugins.
COPY plugins /opt/vrixel/plugin-src
COPY --from=codex /codex /usr/local/bin/codex
# The same plugins, installed for Codex: each Codex session starts from a copy of this home.
RUN export CODEX_HOME=/opt/vrixel/codex-home && mkdir -p "$CODEX_HOME" \
 && codex plugin marketplace add /opt/vrixel/plugins \
 && for p in product-management engineering design vrixel; do codex plugin add "$p@vrixel-plugins"; done \
 && find "$CODEX_HOME" -mindepth 1 -maxdepth 1 ! -name config.toml ! -name plugins -exec rm -rf {} + \
 && chmod -R a+rX "$CODEX_HOME"

RUN useradd --create-home --uid 10001 vrixel && mkdir -p /data/artifacts && chown -R vrixel /data
USER vrixel

EXPOSE 8080
CMD ["uvicorn", "--factory", "vrixel_sdlc.api.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
