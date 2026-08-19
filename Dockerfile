# syntax=docker/dockerfile:1.7

# ---- builder: install deps into an isolated venv ----------------------------
FROM ghcr.io/astral-sh/uv:0.11.0 AS uv

FROM python:3.12-slim AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Only copy dependency metadata first to maximize layer cache.
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    RCM_HOST=0.0.0.0 \
    RCM_PORT=8000 \
    RCM_CONFIG=/app/commands.yaml \
    RCM_RUNS_DIR=/data/runs

# tini for proper PID 1 signal handling; curl for the HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /var/tmp/*

# Non-root user owning /app and /data.
RUN groupadd --system --gid 1000 rcm \
    && useradd --system --uid 1000 --gid rcm --home-dir /app --shell /usr/sbin/nologin rcm

WORKDIR /app

# Bring in the prebuilt venv.
COPY --from=builder /opt/venv /opt/venv

# Copy only what the server needs at runtime.
COPY --chown=rcm:rcm rcm/ ./rcm/
COPY --chown=rcm:rcm run_rcm.py ./

# Persistent run-output directory; mount a volume here in production.
RUN mkdir -p /data/runs && chown -R rcm:rcm /data

USER rcm

EXPOSE 8000
VOLUME ["/data/runs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${RCM_PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "rcm"]
