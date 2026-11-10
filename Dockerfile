# syntax=docker/dockerfile:1.7
# Stage 1: build the web interface (React + Vite) into web/dist.
FROM node:26-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

# Stage 2: the plugins whose skills the agents follow: Anthropic's Product Management, Engineering
# and Design plugins (Apache 2.0) at a pinned commit, and ours (plugins/vrixel). CI checks that
# every skill the phases name is in them; bump the commit in a PR so that check runs.
FROM alpine:3.22 AS plugins
ARG KNOWLEDGE_WORK_PLUGINS_REF=e605d8ddbfb688da57dfaaceebd93f12a64ef1d3
ADD --keep-git-dir=false https://github.com/anthropics/knowledge-work-plugins.git#${KNOWLEDGE_WORK_PLUGINS_REF} /upstream
RUN mkdir /plugins \
 && for p in product-management engineering design; do cp -r "/upstream/$p" /plugins/; done \
 && cp /upstream/LICENSE /plugins/LICENSE.knowledge-work-plugins
COPY plugins/vrixel /plugins/vrixel

# Stage 3: the API, which also serves the built web interface.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VRIXEL_ROOT=/app \
    VRIXEL_ARTIFACT_DIR=/data/artifacts \
    VRIXEL_SKILLS_DIR=/opt/vrixel/plugins

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[live,github,integrations]"
COPY config ./config
# Sample governance repos, used only when VRIXEL_GOVERNANCE_SOURCE=local.
COPY examples ./examples
COPY --from=web /web/dist ./web/dist
COPY --from=plugins /plugins /opt/vrixel/plugins

RUN useradd --create-home --uid 10001 vrixel && mkdir -p /data/artifacts && chown -R vrixel /data
USER vrixel

EXPOSE 8080
CMD ["uvicorn", "--factory", "vrixel_sdlc.api.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
