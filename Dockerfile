# Stage 1: build the web interface (React + Vite) into web/dist.
FROM node:26-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

# Stage 2: the API, which also serves the built web interface.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VRIXEL_ROOT=/app \
    VRIXEL_ARTIFACT_DIR=/data/artifacts

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[github,integrations]"
COPY config ./config
# Sample governance repos, used only when VRIXEL_GOVERNANCE_SOURCE=local.
COPY examples ./examples
COPY --from=web /web/dist ./web/dist

RUN useradd --create-home --uid 10001 vrixel && mkdir -p /data/artifacts && chown -R vrixel /data
USER vrixel

EXPOSE 8080
# One replica: live updates (server-sent events) and intake drafts are held in process.
CMD ["uvicorn", "--factory", "vrixel_sdlc.api.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
