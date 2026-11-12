# The proxy serves the prebuilt SPA assets from disk, so the frontend build happens in
# this image too. docker-compose.yml uses the repository root as the build context, and
# only the dist output is copied into the nginx layer - node/npm never reach the runtime.
FROM node:24-alpine AS frontend

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json /app/frontend/
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline

COPY frontend /app/frontend
RUN --mount=type=cache,target=/root/.npm npm run build


FROM nginx:1.31-alpine

RUN apk add --no-cache apache2-utils

# Same checkout as the backend image, so the hashed asset names the prerendered pages
# reference always exist here. The build context is the repository root (docker-compose.yml
# and any `docker build -f nginx/Dockerfile .`), so paths are relative to that root.
COPY --from=frontend /app/dist /srv/frontend/dist
COPY nginx/entrypoint.sh /entrypoint.sh
COPY nginx/nginx.conf /etc/nginx/nginx.conf
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
