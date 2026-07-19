# syntax=docker/dockerfile:1

FROM python:3.11.15-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=9900 \
    PRODUCTION_EXPOSURE_STRICT=true

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY docs/knowledge-base ./docs/knowledge-base
COPY config ./config
COPY static ./static

RUN uv sync --locked --no-dev --no-editable

RUN addgroup --system autooncall \
    && adduser --system --ingroup autooncall --home /app autooncall \
    && mkdir -p /app/data /app/logs /app/uploads \
    && chown -R autooncall:autooncall /app

USER autooncall

EXPOSE 9900

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health/live', timeout=4)"

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${HOST}\" --port \"${PORT}\""]
