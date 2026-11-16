# Base images are pinned by digest, not by tag: a tag names whatever upstream publishes next, so a
# rebuild - or anyone with registry write access - changes what the image contains without changing
# this file. Renovate proposes digest updates (.github/renovate.json), so a bump is a reviewed PR.
FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Node/npm live in the dedicated frontend stage below; the Python builder only needs the
# toolchain for compiled wheels.
RUN apk add --no-cache \
        build-base \
        cargo \
        openssl-dev \
        pkgconf

# uv is pinned to one exact version so the toolchain does not drift with whatever PyPI publishes
# next; Renovate's managers read the lockfile and the image references, not a pip install, so a bump
# here is a deliberate change to this file.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools \
    && pip install uv==0.10.8

COPY pyproject.toml uv.lock README.md /app/
COPY i3x_server /app/i3x_server

# Registry builds pass `INSTALL_OTEL=true` (.github/workflows/docker.yml), so the published image
# can export the traces docker-compose.yml advertises. The default keeps a lean image for
# deployments that do not enable tracing.
ARG INSTALL_OTEL=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_OTEL" = "true" ]; then \
        uv sync --no-dev --frozen --extra otel; \
    else \
        uv sync --no-dev --frozen; \
    fi


FROM node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS frontend

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json /app/frontend/
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline

COPY frontend /app/frontend
RUN --mount=type=cache,target=/root/.npm npm run build


FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS runtime

ARG BUILD_VERSION=master

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apk add --no-cache \
        ca-certificates \
        tini \
    && apk upgrade --no-cache \
    && rm -rf \
        /usr/local/lib/python3.15/site-packages/pip \
        /usr/local/lib/python3.15/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.15/ensurepip \
    && rm -f \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.15 \
    && addgroup -g 10001 -S app \
    && adduser -u 10001 -S -D -H -G app app

COPY --from=builder /app/.venv /app/.venv
COPY i3x_server /app/i3x_server
COPY --from=frontend /app/dist /app/dist
COPY static /app/static
# MCP tool/prompt overrides are resolved relative to /app/overrides; without this the
# shipped image silently runs with the built-in definitions.
COPY overrides /app/overrides
RUN printf "%s\n" "${BUILD_VERSION}" > /app/server-version.txt \
    && chown app:app /app/server-version.txt

USER app

EXPOSE 8000

# /health is static liveness: it never touches the OPC UA connection, so a reconnecting server
# cannot get a working container killed. Readiness - model built and the upstream Connected - is
# /ready, which an orchestrator probes; wget keeps the probe from spawning a Python interpreter
# every 30s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8000/health

ENTRYPOINT ["/sbin/tini", "--"]
# --limit-concurrency sheds load with 503 instead of piling requests onto the single
# event loop. Workers stay at 1 on purpose: subscription state and MCP sessions are
# process-local, so multiple workers would strand them (see docs/SCALING_GUIDE.md).
# --timeout-graceful-shutdown bounds uvicorn's wait for in-flight requests; the lifespan
# cleanup that runs after it (task cancellation, monitored-node releases, MCP/SSE drain,
# upstream disconnect) needs a container grace period on top of that drain - compose sets
# stop_grace_period: 45s, because Docker's default 10s would SIGKILL mid-cleanup.
CMD ["python", "-m", "uvicorn", "i3x_server.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "200", "--timeout-graceful-shutdown", "30"]
