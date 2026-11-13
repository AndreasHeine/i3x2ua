FROM python:3.15.2-alpine AS builder

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

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools \
    && pip install uv

COPY pyproject.toml uv.lock README.md /app/
COPY i3x_server /app/i3x_server

ARG INSTALL_OTEL=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_OTEL" = "true" ]; then \
        uv sync --no-dev --frozen --extra otel; \
    else \
        uv sync --no-dev --frozen; \
    fi


FROM node:26-alpine AS frontend

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json /app/frontend/
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline

COPY frontend /app/frontend
RUN --mount=type=cache,target=/root/.npm npm run build


FROM python:3.15.2-alpine AS runtime

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

# /health is a static readiness route; probing it avoids spawning a Python interpreter
# every 30s and does not touch the OPC UA connection.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8000/health

ENTRYPOINT ["/sbin/tini", "--"]
# --limit-concurrency sheds load with 503 instead of piling requests onto the single
# event loop. Workers stay at 1 on purpose: subscription state and MCP sessions are
# process-local, so multiple workers would strand them (see docs/SCALING_GUIDE.md).
CMD ["python", "-m", "uvicorn", "i3x_server.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "200"]
