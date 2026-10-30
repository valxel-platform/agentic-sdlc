FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VRIXEL_ROOT=/app \
    VRIXEL_ARTIFACT_DIR=/data/artifacts

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY constitutions ./constitutions

RUN useradd --create-home --uid 10001 vrixel && mkdir -p /data/artifacts && chown -R vrixel /data
USER vrixel

EXPOSE 8080
CMD ["uvicorn", "vrixel_sdlc.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
