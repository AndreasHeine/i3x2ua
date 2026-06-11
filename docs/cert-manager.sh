#!/bin/bash

# Certificate Management Helper Script
# This script helps manage SSL certificates for production deployment

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
CERT_DIR="${1:-.}/certs"
DOMAIN="${2:-your-domain.com}"
DAYS_WARNING="${3:-30}"

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
        return 1
    fi
    return 0
}

# Check certificate expiration
check_expiration() {
    if ! check_certificate; then
        return 1
    fi
    
    local expiry_date=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -enddate | cut -d= -f2)
    local expiry_epoch=$(date -d "$expiry_date" +%s)
    local now_epoch=$(date +%s)
    local days_left=$(( ($expiry_epoch - $now_epoch) / 86400 ))
    
    log_info "Certificate expires on: $expiry_date"
    log_info "Days remaining: $days_left"
    
    if [ $days_left -lt 0 ]; then
        log_error "Certificate has EXPIRED!"
        return 2
    elif [ $days_left -lt $DAYS_WARNING ]; then
        log_warn "Certificate expires in $days_left days - renewal recommended"
        return 1
    else
        log_info "Certificate is valid"
        return 0
    fi
}

# Verify certificate chain
verify_chain() {
    if ! check_certificate; then
        return 1
    fi
    
    log_info "Verifying certificate chain..."
    if openssl verify -CAfile "$CERT_DIR/fullchain.pem" "$CERT_DIR/fullchain.pem" > /dev/null 2>&1; then
        log_info "Certificate chain verification: OK"
        return 0
    else
        log_error "Certificate chain verification FAILED"
        return 1
    fi
}

# Display certificate info
show_info() {
    if ! check_certificate; then
        return 1
    fi
    
    log_info "Certificate Information:"
    openssl x509 -in "$CERT_DIR/fullchain.pem" -text -noout
}

# Check certificate matches domain
check_domain() {
    if ! check_certificate; then
        return 1
    fi
    
    log_info "Checking if certificate matches domain: $DOMAIN"
    
    local cn=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject | grep -oP '(?<=CN\=)[^,]*' || true)
    local san=$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -text | grep -oP '(?<=DNS:)[^,]*' || true)
    
    if echo "$cn $san" | grep -qi "$DOMAIN"; then
        log_info "Certificate matches domain: $DOMAIN"
        return 0
    else
        log_warn "Certificate may not match domain: $DOMAIN"
        log_info "Certificate CN: $cn"
        log_info "Certificate SANs: $san"
        return 1
    fi
}

# Generate self-signed certificate (development only)
generate_selfsigned() {
    log_warn "Generating self-signed certificate for development only!"
    
    mkdir -p "$CERT_DIR"
    
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$CERT_DIR/privkey.pem" \
        -out "$CERT_DIR/fullchain.pem" \
        -days 365 -nodes \
        -subj "/CN=$DOMAIN"
    
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
    
    mkdir -p "$CERT_DIR"
    cp "$le_path/fullchain.pem" "$CERT_DIR/"
    cp "$le_path/privkey.pem" "$CERT_DIR/"
    
    chmod 400 "$CERT_DIR/privkey.pem"
    
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

Usage: $0 [COMMAND] [OPTIONS]

Commands:
  check             Check certificate status
  info              Display certificate information
  verify            Verify certificate chain
  domain            Check if certificate matches domain
  test              Test certificate details
  generate-self     Generate self-signed certificate (dev only)
  from-letsencrypt  Copy Let's Encrypt certificate
  renewal           Check expiration and renewal status
  help              Show this help message

Options:
  --cert-dir DIR    Path to certificates directory (default: ./certs)
  --domain DOMAIN   Domain name (default: your-domain.com)
  --days DAYS       Days warning threshold (default: 30)

Examples:
  $0 check --cert-dir ./certs
  $0 info --domain api.mycompany.com
  $0 generate-self --cert-dir ./certs --domain api.mycompany.com
  $0 from-letsencrypt --domain api.mycompany.com --cert-dir ./certs

EOF
}

# Parse arguments
case "${1:-help}" in
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
        log_error "Unknown command: $1"
        usage
        exit 1
        ;;
esac

exit $?
