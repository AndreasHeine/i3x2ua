#!/bin/sh
set -eu

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

is_positive_integer() {
  case "${1:-}" in
    ''|*[!0-9]*)
      return 1
      ;;
    0)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

upstream_host="${NGINX_UPSTREAM_HOST:-i3x2ua}"
upstream_port="${NGINX_UPSTREAM_PORT:-8000}"
server_name="${NGINX_SERVER_NAME:-_}"
http_port="${NGINX_HTTP_PORT:-80}"
https_port="${NGINX_HTTPS_PORT:-443}"
basic_auth_enabled="${NGINX_BASIC_AUTH_ENABLED:-0}"
https_enabled="${NGINX_HTTPS_ENABLED:-0}"
# Container runs read_only; conf.d is the only writable path available for the generated auth file.
auth_file="/etc/nginx/conf.d/.htpasswd"
cert_file="${NGINX_SSL_CERTIFICATE:-/etc/nginx/certs/fullchain.pem}"
key_file="${NGINX_SSL_CERTIFICATE_KEY:-/etc/nginx/certs/privkey.pem}"
realm="${NGINX_BASIC_AUTH_REALM:-i3x2ua}"
redirect_https_port="${NGINX_HTTPS_REDIRECT_PORT:-443}"
# Prebuilt SPA assets baked into this image (same checkout as the backend image).
frontend_dist="${NGINX_FRONTEND_DIST:-/srv/frontend/dist}"
# Edge admission control. The zones themselves live in nginx.conf (they must be in the http
# context); these knobs decide whether the server blocks apply them and how much slack a client
# gets before further requests answer 429.
rate_limit_enabled="${NGINX_RATE_LIMIT_ENABLED:-1}"
rate_limit_burst="${NGINX_RATE_LIMIT_BURST:-40}"
rate_limit_conn="${NGINX_RATE_LIMIT_CONN_PER_CLIENT:-64}"

if [ "$redirect_https_port" = "443" ]; then
  https_redirect_target="https://\$host\$request_uri"
else
  https_redirect_target="https://\$host:$redirect_https_port\$request_uri"
fi

auth_block=""
if is_truthy "$basic_auth_enabled"; then
  if [ -z "${NGINX_BASIC_AUTH_USER:-}" ] || [ -z "${NGINX_BASIC_AUTH_PASSWORD:-}" ]; then
    echo "NGINX_BASIC_AUTH_USER and NGINX_BASIC_AUTH_PASSWORD are required when NGINX_BASIC_AUTH_ENABLED is enabled" >&2
    exit 1
  fi

  htpasswd -cbB "$auth_file" "$NGINX_BASIC_AUTH_USER" "$NGINX_BASIC_AUTH_PASSWORD" >/dev/null
  auth_block=$(cat <<EOF
      auth_basic "$realm";
      auth_basic_user_file $auth_file;
EOF
)
fi

upstream_block=$(cat <<EOF
upstream i3x2ua_backend {
  server $upstream_host:$upstream_port;
  keepalive 32;
  keepalive_requests 1000;
  keepalive_timeout 60s;
}
EOF
)

proxy_block=$(cat <<EOF
      proxy_http_version 1.1;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_set_header X-Forwarded-Host \$host;
      proxy_set_header X-Forwarded-Port \$server_port;
      proxy_set_header Connection "";
      proxy_connect_timeout 5s;
      proxy_send_timeout 60s;
      proxy_read_timeout 60s;
      proxy_pass http://i3x2ua_backend;
EOF
)

sse_proxy_block=$(cat <<EOF
      proxy_http_version 1.1;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_set_header X-Forwarded-Host \$host;
      proxy_set_header X-Forwarded-Port \$server_port;
      proxy_set_header Connection "";
      proxy_buffering off;
      proxy_cache off;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_pass http://i3x2ua_backend;
EOF
)

# MCP streamable HTTP (GET /mcp) is a long-lived event stream; buffering it hides
# tool events and the 60s proxy_read_timeout would cut idle streams. Sessions are
# process-local, so /mcp must reach one backend - the same upstream as everything else.
mcp_proxy_block=$(cat <<EOF
      proxy_http_version 1.1;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_set_header X-Forwarded-Host \$host;
      proxy_set_header X-Forwarded-Port \$server_port;
      proxy_set_header Connection "";
      proxy_buffering off;
      proxy_cache off;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      chunked_transfer_encoding on;
      proxy_pass http://i3x2ua_backend;
EOF
)

# Framing is refused at the edge too, matching the CSP's frame-ancestors 'none' that the app sets
# on its HTML responses. "always" covers nginx-generated error pages (429/413), which the app
# never sees.
security_headers_block=$(cat <<EOF
      add_header X-Frame-Options "DENY" always;
EOF
)

# HSTS only makes sense where TLS is the only entry point, so it is added in the HTTPS server.
hsts_block=""
if is_truthy "$https_enabled"; then
  hsts_block=$(cat <<EOF
      add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
EOF
)
fi

# Applied per server block, so every location inherits it. A location that sets its own
# add_header (the asset cache headers) does not inherit these, so it repeats them.
limit_block=""
if is_truthy "$rate_limit_enabled"; then
  if ! is_positive_integer "$rate_limit_burst" || ! is_positive_integer "$rate_limit_conn"; then
    echo "NGINX_RATE_LIMIT_BURST and NGINX_RATE_LIMIT_CONN_PER_CLIENT must be positive integers" >&2
    exit 1
  fi
  limit_block=$(cat <<EOF
      limit_req zone=i3x_client burst=$rate_limit_burst nodelay;
      limit_conn i3x_conn $rate_limit_conn;
EOF
)
fi

mkdir -p /etc/nginx/conf.d

# Content-hashed SPA assets ("_app/immutable/...") are served straight from the dist
# directory instead of a uvicorn static-file read plus gzip per page load. Hashed
# filenames change with every build, so "immutable" edge caching is safe; version.json
# keeps a fixed name and must never be cached. Files missing from this image's dist fall
# back to the backend, which serves the same dist. Assets carry no secrets, so they stay
# unauthenticated even when Basic Auth protects the pages and API.
asset_block=""
if [ -d "$frontend_dist/_app" ]; then
  asset_block=$(cat <<EOF

  location = /_app/version.json {
    root $frontend_dist;
    add_header Cache-Control "public, no-store";
$(printf '%s\n' "$security_headers_block")
    try_files \$uri @frontend_asset_fallback;
  }

  location ^~ /_app/ {
    root $frontend_dist;
    expires max;
    add_header Cache-Control "public, immutable";
$(printf '%s\n' "$security_headers_block")
    try_files \$uri @frontend_asset_fallback;
  }

  location @frontend_asset_fallback {
$(printf '%s\n' "$proxy_block")
  }
EOF
)
fi

if is_truthy "$https_enabled"; then
  if [ ! -r "$cert_file" ] || [ ! -r "$key_file" ]; then
    echo "NGINX_HTTPS_ENABLED is enabled but certificate or key file is missing" >&2
    echo "Expected cert: $cert_file" >&2
    echo "Expected key:  $key_file" >&2
    exit 1
  fi

  cat > /etc/nginx/conf.d/default.conf <<EOF
$(printf '%s\n' "$upstream_block")

server {
  listen $http_port;
  server_name $server_name;
$(printf '%s\n' "$security_headers_block")
$(printf '%s\n' "$limit_block")
  return 301 $https_redirect_target;
}

server {
  listen $https_port ssl;
  http2 on;
  server_name $server_name;
  ssl_certificate $cert_file;
  ssl_certificate_key $key_file;
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_prefer_server_ciphers on;
$(printf '%s\n' "$security_headers_block")
$(printf '%s\n' "$hsts_block")
$(printf '%s\n' "$limit_block")

  location = /v1/info {
$(printf '%s\n' "$proxy_block")
  }

  location = /v1/subscriptions/stream {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$sse_proxy_block")
  }

  location = /mcp {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$mcp_proxy_block")
  }

$(printf '%s\n' "$asset_block")
  location / {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$proxy_block")
  }
}
EOF
else
  cat > /etc/nginx/conf.d/default.conf <<EOF
$(printf '%s\n' "$upstream_block")

server {
  listen $http_port;
  server_name $server_name;
$(printf '%s\n' "$security_headers_block")
$(printf '%s\n' "$limit_block")

  location = /v1/info {
$(printf '%s\n' "$proxy_block")
  }

  location = /v1/subscriptions/stream {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$sse_proxy_block")
  }

  location = /mcp {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$mcp_proxy_block")
  }

$(printf '%s\n' "$asset_block")
  location / {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$proxy_block")
  }
}
EOF
fi

exec nginx -g 'daemon off;'
