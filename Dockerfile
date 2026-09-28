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

# Engine + serve (mcp[cli], uvicorn, starlette) + events (pymongo) + solana (solders,
# for the hosted /ore/mcp Program Surface's derive_pda). --no-dev drops mypy/pytest/ruff;
# --frozen pins to uv.lock (must resolve every extra).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra serve --extra events --extra solana

# -----------------------------------------------------------------------------
# The COURSE corpus for /course/mcp: the PUBLISHED cohort repository, the same files a
# student clones, so no solution or answer key can reach the image by this path.
# The course surface refuses to mount without a corpus, which is how production served
# a 404 at /course/mcp on 2026-09-28: the env was unset and the files were never here.
# COURSE_REF is a commit, pinned by infra/deploy.sh from the cohort's HEAD, so the
# layer cache cannot serve last week's course under an unchanged "main".

FROM python:3.12-slim AS course
ARG COURSE_REF=main
RUN python -c "import io, tarfile, urllib.request; \
url = 'https://codeload.github.com/Gecko-Academy/dev3pack-cohort-2026-09/tar.gz/${COURSE_REF}'; \
tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url, timeout=120).read())).extractall('/tmp/c', filter='data')" \
    && mv /tmp/c/* /course

# -----------------------------------------------------------------------------

FROM python:3.12-slim AS runner

RUN useradd --create-home --shell /bin/bash gecko

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/pyproject.toml /app/uv.lock /app/README.md ./
COPY gecko ./gecko
# Every example surface spec, served by gecko.serve_mcp / gecko.serve_providers.
# Copy the whole tree — a per-surface COPY silently missed examples/jito/spec once
# and crashed serve_mcp at startup (FileNotFoundError), stalling a deploy. Copying
# examples/ wholesale means a new surface's spec can never be left out of the image.
# Control plane only: no payloads, no secrets.
COPY examples ./examples
COPY --from=course /course ./course

RUN chown -R gecko:gecko /app
USER gecko

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    GECKO_COURSE_ROOT=/app/course

EXPOSE 8000

# Mirror the ALB target-group health check (GET /healthz, matcher 200) locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)" || exit 1

# Bind 0.0.0.0:$PORT, mode=live, allowlist mcp.geckovision.tech.
CMD ["python", "-m", "gecko.serve_mcp"]
