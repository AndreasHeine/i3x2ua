FROM python:3.15.1-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apk add --no-cache \
        build-base \
        cargo \
        openssl-dev \
        nodejs \
        npm \
        pkgconf

RUN python -m pip install --no-cache-dir --upgrade pip setuptools \
    && pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md /app/
COPY i3x_server /app/i3x_server
COPY frontend /app/frontend

ARG INSTALL_OTEL=false
RUN if [ "$INSTALL_OTEL" = "true" ]; then \
        uv sync --no-dev --frozen --extra otel; \
    else \
        uv sync --no-dev --frozen; \
    fi

RUN cd /app/frontend \
    && npm ci \
    && npm run build


FROM python:3.15.1-alpine AS runtime

ARG BUILD_VERSION=master

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apk add --no-cache \
        ca-certificates \
        tini \
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
COPY --from=builder /app/dist /app/dist
COPY static /app/static
RUN printf "%s\n" "${BUILD_VERSION}" > /app/server-version.txt \
    && chown app:app /app/server-version.txt

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/info', timeout=3)"

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "-m", "uvicorn", "i3x_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
