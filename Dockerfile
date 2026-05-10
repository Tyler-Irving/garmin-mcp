FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv: a fast, reproducible Python installer.
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first to maximise layer caching.
COPY pyproject.toml uv.lock* README.md ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev || uv sync --no-dev

# Cloud Run sets PORT; default for local docker run.
ENV PORT=8080 \
    HOST=0.0.0.0 \
    GARMIN_TOKEN_DIR=/tmp/garth

EXPOSE 8080

# /tmp is writable on Cloud Run; that's where we cache Garmin OAuth tokens.
CMD ["uv", "run", "--no-dev", "garmin-mcp", "serve"]
