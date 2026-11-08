#!/bin/bash

# Production Deployment Helper Script
# Automates common production deployment and management tasks

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_section() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

# Docker Compose is either the standalone binary or the "docker compose" plugin.
run_compose() {
    if command -v docker-compose &> /dev/null; then
        docker-compose "$@"
    else
        docker compose "$@"
    fi
}

# Defaults; overridable with the flags documented in usage().
PROJECT_DIR="."
ENV_FILE=""
CERT_DIR=""
COMPOSE_FILE=""
HTTP_PORT="8080"    # Host port mapped to the nginx HTTP listener (docker-compose.yml).
HTTPS_PORT="8443"   # Host port mapped to the nginx HTTPS listener (docker-compose.yml).

# Check prerequisites
check_prerequisites() {
    log_section "Checking Prerequisites"

    local missing=0

    # Check Docker
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        missing=1
    else
        log_info "✓ Docker $(docker --version)"
    fi

    # Check Docker Compose (standalone binary or the "docker compose" plugin)
    if command -v docker-compose &> /dev/null; then
        log_info "✓ Docker Compose $(docker-compose --version)"
    elif docker compose version &> /dev/null; then
        log_info "✓ Docker Compose $(docker compose version)"
    else
        log_error "Docker Compose is not installed"
        missing=1
    fi

    # Check if .env exists
    if [ ! -f "$ENV_FILE" ]; then
        log_warn ".env file not found at $ENV_FILE"
        log_info "Creating from a shipped template..."
        if [ -f "${PROJECT_DIR}/docs/.env.production.example" ]; then
            cp "${PROJECT_DIR}/docs/.env.production.example" "$ENV_FILE"
            log_info "✓ .env file created (please update with your values)"
        elif [ -f "${PROJECT_DIR}/.env-example" ]; then
            cp "${PROJECT_DIR}/.env-example" "$ENV_FILE"
            log_info "✓ .env file created from .env-example (please update with your values)"
        else
            log_error "No example .env file found"
            missing=1
        fi
    else
        log_info "✓ .env file found"
    fi

    if [ ! -f "$COMPOSE_FILE" ]; then
        log_error "Compose file not found: $COMPOSE_FILE"
        missing=1
    else
        log_info "✓ Compose file found"
    fi

    return $missing
}

# Validate environment
validate_environment() {
    log_section "Validating Environment"

    if [ ! -f "$ENV_FILE" ]; then
        log_error ".env file not found"
        return 1
    fi

    # Source .env
    set -a
    source "$ENV_FILE"
    set +a

    # Check required variables
    local required_vars=(
        "NGINX_HTTPS_ENABLED"
        "I3X_OPCUA_ENDPOINT"
    )

    local missing=0
    for var in "${required_vars[@]}"; do
        if [ -z "${!var}" ]; then
            log_warn "Required variable $var is not set"
            missing=1
        else
            log_info "✓ $var is set"
        fi
    done

    # Check HTTPS settings. The compose mount exposes the host cert directory as
    # /etc/nginx/certs, and the entrypoint defaults below use certs/https-sample.
    if [ "$NGINX_HTTPS_ENABLED" = "1" ]; then
        log_info "HTTPS is enabled"

        if [ ! -d "$CERT_DIR" ]; then
            log_error "Certificate directory $CERT_DIR not found"
            return 1
        fi

        if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
            log_error "Certificate files not found in $CERT_DIR"
            log_info "Generate a development certificate: python scripts/generate_https_dev_cert.py"
            return 1
        fi

        log_info "✓ SSL certificates found"
    fi

    return $missing
}

# Initialize deployment
init_deployment() {
    log_section "Initializing Deployment"

    # Create certificate directory
    mkdir -p "$CERT_DIR"
    log_info "✓ Certificate directory ready"

    # Create necessary directories
    mkdir -p "${PROJECT_DIR}/monitoring"
    log_info "✓ Monitoring directory ready"

    # Check docker daemon
    if ! docker ps > /dev/null 2>&1; then
        log_error "Cannot connect to Docker daemon"
        return 1
    fi
    log_info "✓ Docker daemon is running"

    return 0
}

# Build and start services
start_services() {
    log_section "Starting Services"

    log_info "Building Docker images..."
    run_compose -f "$COMPOSE_FILE" build --no-cache

    log_info "Starting services..."
    run_compose -f "$COMPOSE_FILE" up -d

    log_info "Waiting for services to be healthy..."
    sleep 10

    # Check status
    run_compose -f "$COMPOSE_FILE" ps

    log_info "✓ Services started"

    return 0
}

# Test deployment
test_deployment() {
    log_section "Testing Deployment"

    # Source .env for variables
    set -a
    source "$ENV_FILE"
    set +a

    local protocol="http"
    local port="$HTTP_PORT"
    if [ "$NGINX_HTTPS_ENABLED" = "1" ]; then
        protocol="https"
        port="$HTTPS_PORT"
    fi

    # nginx serves on loopback ports from the shipped compose mapping; the server
    # name is typically "_" (match any host), so probing it would not resolve.
    local url="$protocol://127.0.0.1:$port/v1/info"

    log_info "Testing endpoint: $url"

    if ! command -v curl &> /dev/null; then
        log_warn "curl is not available; cannot verify service response"
        return 1
    fi

    local curl_args=(-f -s -o /dev/null -w "%{http_code}")
    if [ "$NGINX_HTTPS_ENABLED" = "1" ]; then
        # Development certificates are self-signed; accept them in this smoke test.
        curl_args+=(-k)
    fi
    if [ "${NGINX_BASIC_AUTH_ENABLED:-0}" = "1" ]; then
        if [ -z "${NGINX_BASIC_AUTH_USER:-}" ] || [ -z "${NGINX_BASIC_AUTH_PASSWORD:-}" ]; then
            log_error "Basic Auth is enabled but NGINX_BASIC_AUTH_USER/NGINX_BASIC_AUTH_PASSWORD are not set"
            return 1
        fi
        curl_args+=(-u "${NGINX_BASIC_AUTH_USER}:${NGINX_BASIC_AUTH_PASSWORD}")
    fi

    # curl reports the status through -w and exits non-zero for failed transfers
    # (with -f) or connection errors; it already writes "000" when no transfer happened,
    # so the failure path must not append another code.
    local status
    status=$(curl "${curl_args[@]}" "$url" 2>/dev/null) || true
    status="${status:-000}"
    if [ "$status" = "200" ]; then
        log_info "✓ Service is responding (HTTP 200)"
        return 0
    fi

    log_warn "Could not verify service response (HTTP $status)"
    return 1
}

# Display status
show_status() {
    log_section "Deployment Status"

    log_info "Container Status:"
    run_compose -f "$COMPOSE_FILE" ps

    log_info "\nRecent Logs (NGINX):"
    run_compose -f "$COMPOSE_FILE" logs --tail=20 nginx 2>/dev/null || true

    log_info "\nRecent Logs (i3x2ua):"
    run_compose -f "$COMPOSE_FILE" logs --tail=20 i3x2ua 2>/dev/null || true
}

# Backup data
backup_data() {
    log_section "Backing Up Data"

    local backup_dir="${PROJECT_DIR}/backups/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$backup_dir"

    # Backup certificates
    if [ -d "$CERT_DIR" ]; then
        cp -r "$CERT_DIR" "$backup_dir/"
        log_info "✓ Certificates backed up"
    fi

    # Backup .env (without secrets visible)
    cp "$ENV_FILE" "$backup_dir/.env.bak"
    log_info "✓ Configuration backed up"

    log_info "Backups saved to: $backup_dir"
    return 0
}

# Clean up
cleanup() {
    log_section "Cleaning Up"

    read -p "Remove containers? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        run_compose -f "$COMPOSE_FILE" down
        log_info "✓ Containers removed"
    fi

    read -p "Remove volumes? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        run_compose -f "$COMPOSE_FILE" down -v
        log_info "✓ Volumes removed"
    fi
}

# Usage
usage() {
    cat << EOF
Production Deployment Helper Script

Usage: $0 COMMAND [OPTIONS]

Commands:
  init              Initialize and validate deployment
  start             Build and start all services
  test              Test the deployment
  status            Show deployment status
  backup            Backup data and configuration
  clean             Clean up containers and volumes
  full              Run full deployment workflow (init → start → test)
  help              Show this help message

Options:
  --project-dir DIR Path to the project directory (default: .)
  --env-file FILE   Path to the .env file (default: <project-dir>/.env)
  --compose FILE    Path to the compose file (default: <project-dir>/docker-compose.yml)
  --cert-dir DIR    Certificate directory (default: <project-dir>/certs/https-sample)
  --http-port PORT  Host port mapped to the nginx HTTP listener (default: 8080)
  --https-port PORT Host port mapped to the nginx HTTPS listener (default: 8443)

Examples:
  $0 init
  $0 start
  $0 full --project-dir /path/to/project
  $0 test --https-port 8443
  $0 status

EOF
}

# Parse options after the command
parse_options() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --project-dir)
                PROJECT_DIR="$2"
                shift 2
                ;;
            --env-file)
                ENV_FILE="$2"
                shift 2
                ;;
            --compose)
                COMPOSE_FILE="$2"
                shift 2
                ;;
            --cert-dir)
                CERT_DIR="$2"
                shift 2
                ;;
            --http-port)
                HTTP_PORT="$2"
                shift 2
                ;;
            --https-port)
                HTTPS_PORT="$2"
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
main() {
    local command="${1:-help}"
    shift || true

    parse_options "$@"

    # Defaults derived from the resolved project directory, so flags given before
    # or after the command still apply consistently.
    ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
    CERT_DIR="${CERT_DIR:-${PROJECT_DIR}/certs/https-sample}"
    COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.yml}"

    case "$command" in
        init)
            check_prerequisites && init_deployment && validate_environment
            ;;
        start)
            validate_environment && start_services
            ;;
        test)
            test_deployment
            ;;
        status)
            show_status
            ;;
        backup)
            backup_data
            ;;
        clean)
            cleanup
            ;;
        full)
            check_prerequisites && init_deployment && validate_environment && start_services && sleep 5 && test_deployment
            ;;
        help|--help|-h)
            usage
            ;;
        *)
            log_error "Unknown command: $command"
            usage
            exit 1
            ;;
    esac
}

main "$@"
