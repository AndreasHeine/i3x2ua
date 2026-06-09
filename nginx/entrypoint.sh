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

upstream_host="${NGINX_UPSTREAM_HOST:-i3x2ua}"
upstream_port="${NGINX_UPSTREAM_PORT:-8000}"
server_name="${NGINX_SERVER_NAME:-_}"
basic_auth_enabled="${NGINX_BASIC_AUTH_ENABLED:-0}"
https_enabled="${NGINX_HTTPS_ENABLED:-0}"
auth_file="/etc/nginx/.htpasswd"
cert_file="${NGINX_SSL_CERTIFICATE:-/etc/nginx/certs/fullchain.pem}"
key_file="${NGINX_SSL_CERTIFICATE_KEY:-/etc/nginx/certs/privkey.pem}"
realm="${NGINX_BASIC_AUTH_REALM:-i3x2ua}"

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

proxy_block=$(cat <<EOF
      proxy_http_version 1.1;
      proxy_set_header Host \$host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_set_header X-Forwarded-Host \$host;
      proxy_set_header X-Forwarded-Port \$server_port;
      proxy_set_header Connection "";
      proxy_pass http://$upstream_host:$upstream_port;
EOF
)

mkdir -p /etc/nginx/conf.d

if is_truthy "$https_enabled"; then
  if [ ! -r "$cert_file" ] || [ ! -r "$key_file" ]; then
    echo "NGINX_HTTPS_ENABLED is enabled but certificate or key file is missing" >&2
    echo "Expected cert: $cert_file" >&2
    echo "Expected key:  $key_file" >&2
    exit 1
  fi

  cat > /etc/nginx/conf.d/default.conf <<EOF
server {
  listen 80;
  server_name $server_name;
  return 301 https://\$host\$request_uri;
}

server {
  listen 443 ssl http2;
  server_name $server_name;
  ssl_certificate $cert_file;
  ssl_certificate_key $key_file;
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_prefer_server_ciphers on;

  location / {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$proxy_block")
  }
}
EOF
else
  cat > /etc/nginx/conf.d/default.conf <<EOF
server {
  listen 80;
  server_name $server_name;

  location / {
$(printf '%s\n' "$auth_block")
$(printf '%s\n' "$proxy_block")
  }
}
EOF
fi

exec nginx -g 'daemon off;'
