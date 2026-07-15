# syntax=docker/dockerfile:1

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=9900 \
    PRODUCTION_EXPOSURE_STRICT=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY docs/knowledge-base ./docs/knowledge-base
COPY config ./config
COPY static ./static

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN addgroup --system autooncall \
    && adduser --system --ingroup autooncall --home /app autooncall \
    && mkdir -p /app/data /app/logs /app/uploads \
    && chown -R autooncall:autooncall /app

USER autooncall

EXPOSE 9900

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health/live || exit 1

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${HOST}\" --port \"${PORT}\""]
