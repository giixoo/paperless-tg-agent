FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY README.md ./

RUN uv sync --frozen --no-dev


FROM python:3.12-slim

RUN groupadd --gid 1000 paperbot && useradd --uid 1000 --gid paperbot --create-home paperbot

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN mkdir -p /data && chown paperbot:paperbot /data

USER paperbot

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"HEALTH_PORT\",\"8080\")}/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "paperbot"]
