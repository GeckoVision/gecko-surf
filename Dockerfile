# =============================================================================
# gecko-surf — MCP Streamable-HTTP container (Python + uv + uvicorn on port 8000)
#
# Single-package repo (NOT a uv workspace). Stage 1 builds the venv with the
# `serve` extra (mcp[cli] + uvicorn -> pulls starlette) and the `events` extra
# (pymongo — the hosted usage-events sink; a no-op unless MONGODB_URI is set).
# Stage 2 copies the venv + the gecko package + the one local OpenAPI spec the
# container serves. Control plane only: no payloads/secrets at build time;
# MONGODB_URI arrives at runtime via ECS Secrets -> SSM.
# =============================================================================

FROM python:3.12-slim AS builder

# uv (Astral): static binary from the official image (slim has no curl/wget).
COPY --from=ghcr.io/astral-sh/uv:0.5.30 /uv /usr/local/bin/uv

WORKDIR /app

# Manifests + source first, so `uv sync` can build the flat-layout package.
COPY pyproject.toml uv.lock README.md ./
COPY gecko ./gecko

# Engine + serve (mcp[cli], uvicorn, starlette) + events (pymongo). --no-dev
# drops mypy/pytest/ruff; --frozen pins to uv.lock (must resolve both extras).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra serve --extra events

# -----------------------------------------------------------------------------

FROM python:3.12-slim AS runner

RUN useradd --create-home --shell /bin/bash gecko

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/pyproject.toml /app/uv.lock /app/README.md ./
COPY gecko ./gecko
# The one OpenAPI spec the container comprehends + serves. Control plane only:
# no payloads, no secrets.
COPY examples/sos_vzla_bot/spec ./examples/sos_vzla_bot/spec

RUN chown -R gecko:gecko /app
USER gecko

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

# Mirror the ALB target-group health check (GET /healthz, matcher 200) locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)" || exit 1

# Bind 0.0.0.0:$PORT, mode=live, allowlist mcp.geckovision.tech.
CMD ["python", "-m", "gecko.serve_mcp"]
