#!/bin/bash

# Certificate Management Helper Script
# This script helps manage SSL certificates for production deployment

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Defaults; overridable with the flags documented in usage(). The shipped compose
# mount exposes ./certs as /etc/nginx/certs and the entrypoint uses certs/https-sample.
CERT_DIR="./certs/https-sample"
DOMAIN="your-domain.com"
DAYS_WARNING="30"

# Functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if certificate exists
check_certificate() {
    if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
        log_error "Certificate files not found in $CERT_DIR"
        log_info "Generate a development certificate: python scripts/generate_https_dev_cert.py"
        return 1
    fi
    return 0
}

# Days remaining until expiry, computed portably (no GNU date extensions).
days_left() {
    if ! command -v python3 &> /dev/null; then
        log_warn "python3 is not available; cannot compute days remaining"
        return 3
    fi
    local enddate
    enddate=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -enddate | cut -d= -f2)
    python3 -c 'import ssl, sys, time
print(int((ssl.cert_time_to_seconds(sys.argv[1]) - time.time()) / 86400))' "$enddate"
}

# Check certificate expiration
check_expiration() {
    if ! check_certificate; then
        return 1
    fi

    local expiry_date=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -enddate | cut -d= -f2)
    local days_left_value
    days_left_value=$(days_left) || return 1

    log_info "Certificate expires on: $expiry_date"

    if [ "$days_left_value" = "unknown" ]; then
        return 3
    fi
    log_info "Days remaining: $days_left_value"

    if [ "$days_left_value" -lt 0 ]; then
        log_error "Certificate has EXPIRED!"
        return 2
    elif [ "$days_left_value" -lt "$DAYS_WARNING" ]; then
        log_warn "Certificate expires in $days_left_value days - renewal recommended"
        return 1
    else
        log_info "Certificate is valid"
        return 0
    fi
}

# Certificate and private key must belong to the same key pair.
check_key_pair() {
    local cert_pub key_pub
    cert_pub=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -pubkey)
    key_pub=$(openssl pkey -pubout -in "$CERT_DIR/privkey.pem")

    if [ "$cert_pub" = "$key_pub" ]; then
        log_info "Private key matches the certificate public key"
        return 0
    fi

    log_error "Private key does not match the certificate public key"
    return 1
}

# Verify certificate chain
verify_chain() {
    if ! check_certificate; then
        return 1
    fi

    log_info "Certificate subject: $(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject)"
    log_info "Certificate issuer:  $(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -issuer)"

    if ! check_key_pair; then
        return 1
    fi

    # A self-signed certificate cannot be verified against a public CA store; that is
    # expected for the development certificate shipped with this repository.
    local issuer subject
    issuer=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -issuer)
    subject=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject)
    if [ "${issuer#issuer=}" = "${subject#subject=}" ]; then
        log_warn "Self-signed certificate - not verifiable against a public CA (development only)"
        return 0
    fi

    local ca_bundle=""
    for candidate in /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem; do
        if [ -r "$candidate" ]; then
            ca_bundle="$candidate"
            break
        fi
    done

    if [ -z "$ca_bundle" ]; then
        log_warn "No system CA bundle found; skipping chain verification"
        return 0
    fi

    if openssl verify -CAfile "$ca_bundle" "$CERT_DIR/fullchain.pem" > /dev/null 2>&1; then
        log_info "Certificate chain verification: OK (against $ca_bundle)"
        return 0
    fi

    log_error "Certificate chain verification FAILED (issuer chain incomplete or untrusted)"
    return 1
}

# Display certificate info
show_info() {
    if ! check_certificate; then
        return 1
    fi

    log_info "Certificate Information:"
    openssl x509 -in "$CERT_DIR/fullchain.pem" -text -noout
}

# Extract certificate names (CN and SAN DNS entries), portable without GNU grep.
# "ssl_san" is not a valid -nameopt option on OpenSSL >= 3.0, so the SAN extension is
# read separately with -ext subjectAltName.
certificate_names() {
    openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject -nameopt multiline \
        | sed -n 's/^[[:space:]]*commonName[[:space:]]*=[[:space:]]*/CN=/p'
    openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -ext subjectAltName 2>/dev/null \
        | tr ',' '\n' \
        | sed -n 's/.*DNS:[[:space:]]*\([^,]*\).*/DNS=\1/p'
}

# Check certificate matches domain
check_domain() {
    if ! check_certificate; then
        return 1
    fi

    log_info "Checking if certificate matches domain: $DOMAIN"

    local names
    names=$(certificate_names)

    if echo "$names" | grep -qi "$DOMAIN"; then
        log_info "Certificate matches domain: $DOMAIN"
        return 0
    fi

    log_warn "Certificate may not match domain: $DOMAIN"
    log_info "Certificate names:"
    echo "$names"
    return 1
}

# Generate self-signed certificate (development only)
generate_selfsigned() {
    log_warn "Generating self-signed certificate for development only!"

    umask 077
    mkdir -p "$CERT_DIR"

    openssl req -x509 -newkey rsa:4096 \
        -keyout "$CERT_DIR/privkey.pem" \
        -out "$CERT_DIR/fullchain.pem" \
        -days 365 -nodes \
        -subj "/CN=$DOMAIN"

    chmod 600 "$CERT_DIR/privkey.pem"
    log_info "Self-signed certificate generated in $CERT_DIR"
}

# Validate Let's Encrypt certificate
validate_letsencrypt() {
    local le_path="/etc/letsencrypt/live/$DOMAIN"

    if [ ! -d "$le_path" ]; then
        log_error "Let's Encrypt certificate not found at $le_path"
        return 1
    fi

    log_info "Copying Let's Encrypt certificate from $le_path"

    umask 077
    mkdir -p "$CERT_DIR"
    cp "$le_path/fullchain.pem" "$CERT_DIR/"
    cp "$le_path/privkey.pem" "$CERT_DIR/"

    chmod 600 "$CERT_DIR/privkey.pem"

    log_info "Let's Encrypt certificate copied to $CERT_DIR"
}

# Test certificate with OpenSSL
test_certificate() {
    if ! check_certificate; then
        return 1
    fi

    log_info "Testing certificate..."

    # Extract certificate details
    echo "=== Certificate Subject ==="
    openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject

    echo -e "\n=== Certificate Issuer ==="
    openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -issuer

    echo -e "\n=== Valid From ==="
    openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -dates

    echo -e "\n=== Key Info ==="
    openssl pkey -in "$CERT_DIR/privkey.pem" -text -noout | head -3

    return 0
}

# Usage
usage() {
    cat << EOF
Certificate Management Helper Script

Usage: $0 COMMAND [OPTIONS]

Commands:
  check             Check certificate status
  info              Display certificate information
  verify            Verify certificate chain and key pair
  domain            Check if certificate matches domain
  test              Test certificate details
  generate-self     Generate self-signed certificate (dev only)
  from-letsencrypt  Copy Let's Encrypt certificate
  renewal           Check expiration and renewal status
  help              Show this help message

Options:
  --cert-dir DIR    Path to certificates directory (default: ./certs/https-sample)
  --domain DOMAIN   Domain name (default: your-domain.com)
  --days DAYS       Days warning threshold (default: 30)

Examples:
  $0 check --cert-dir ./certs/https-sample
  $0 info --domain api.mycompany.com
  $0 generate-self --cert-dir ./certs/https-sample --domain api.mycompany.com
  $0 from-letsencrypt --domain api.mycompany.com --cert-dir ./certs/https-sample

EOF
}

# Parse options after the command
parse_options() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --cert-dir)
                CERT_DIR="$2"
                shift 2
                ;;
            --domain)
                DOMAIN="$2"
                shift 2
                ;;
            --days)
                DAYS_WARNING="$2"
                shift 2
                ;;
            *)
                log_error "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
    done
}

# Main
COMMAND="${1:-help}"
shift || true
parse_options "$@"

case "$COMMAND" in
    check)
        check_certificate && check_expiration
        ;;
    info)
        show_info
        ;;
    verify)
        verify_chain
        ;;
    domain)
        check_domain
        ;;
    test)
        test_certificate
        ;;
    generate-self)
        generate_selfsigned
        ;;
    from-letsencrypt)
        validate_letsencrypt
        ;;
    renewal)
        check_expiration
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        log_error "Unknown command: $COMMAND"
        usage
        exit 1
        ;;
esac

exit $?
