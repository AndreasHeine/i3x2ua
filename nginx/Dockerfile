# The proxy serves the prebuilt SPA assets from disk, so the frontend build happens in
# this image too. docker-compose.yml uses the repository root as the build context, and
# only the dist output is copied into the nginx layer - node/npm never reach the runtime.
# Digest-pinned like the gateway image (Dockerfile), so the proxy compiles the SPA with the same
# Node the published image uses and neither tag can move underneath a build.
FROM node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS frontend

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json /app/frontend/
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline

COPY frontend /app/frontend
RUN --mount=type=cache,target=/root/.npm npm run build


FROM nginx:1.31-alpine@sha256:7fc0fb4af7bf006aeb31aea88069976060f8f0a0a86b667126742cc17f4e31ae

RUN apk add --no-cache apache2-utils

# Same checkout as the backend image, so the hashed asset names the prerendered pages
# reference always exist here. The build context is the repository root (docker-compose.yml
# and any `docker build -f nginx/Dockerfile .`), so paths are relative to that root.
COPY --from=frontend /app/dist /srv/frontend/dist
COPY nginx/entrypoint.sh /entrypoint.sh
COPY nginx/nginx.conf /etc/nginx/nginx.conf
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
