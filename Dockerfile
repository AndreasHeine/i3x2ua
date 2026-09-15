FROM python:3.14.7-alpine AS builder

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

RUN uv sync --no-dev --frozen

RUN cd /app/frontend \
    && npm ci \
    && npm run build


FROM python:3.14.7-alpine AS runtime

ARG BUILD_VERSION=master

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apk add --no-cache \
        ca-certificates \
        tini \
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

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "i3x_server.main:app", "--host", "0.0.0.0", "--port", "8000"]
