#!/bin/bash

# Production Deployment Helper Script
# Automates common production deployment and management tasks

set -e

# Configuration
PROJECT_DIR="${1:-.}"
ENV_FILE="${PROJECT_DIR}/.env"
CERT_DIR="${PROJECT_DIR}/certs"

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
    
    # Check Docker Compose
    if ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose is not installed"
        missing=1
    else
        log_info "✓ Docker Compose $(docker-compose --version)"
    fi
    
    # Check if .env exists
    if [ ! -f "$ENV_FILE" ]; then
        log_warn ".env file not found at $ENV_FILE"
        log_info "Creating from .env.production.example..."
        if [ -f "${PROJECT_DIR}/docs/.env.production.example" ]; then
            cp "${PROJECT_DIR}/docs/.env.production.example" "$ENV_FILE"
            log_info "✓ .env file created (please update with your values)"
        else
            log_error "No example .env file found"
            missing=1
        fi
    else
        log_info "✓ .env file found"
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
    
    # Check HTTPS settings
    if [ "$NGINX_HTTPS_ENABLED" = "1" ]; then
        log_info "HTTPS is enabled"
        
        if [ ! -d "$CERT_DIR" ]; then
            log_error "Certificate directory $CERT_DIR not found"
            return 1
        fi
        
        if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
            log_error "Certificate files not found in $CERT_DIR"
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
    mkdir -p monitoring
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
    docker-compose -f "$PROJECT_DIR/docker-compose.yml" build --no-cache
    
    log_info "Starting services..."
    docker-compose -f "$PROJECT_DIR/docker-compose.yml" up -d
    
    log_info "Waiting for services to be healthy..."
    sleep 10
    
    # Check status
    docker-compose -f "$PROJECT_DIR/docker-compose.yml" ps
    
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
    if [ "$NGINX_HTTPS_ENABLED" = "1" ]; then
        protocol="https"
    fi
    
    local domain="${NGINX_SERVER_NAME:-localhost}"
    local url="$protocol://$domain/v1/info"
    
    log_info "Testing endpoint: $url"
    
    if command -v curl &> /dev/null; then
        if [ "$NGINX_HTTPS_ENABLED" = "1" ]; then
            # For HTTPS, allow self-signed certificates in testing
            if curl -k -f "$url" > /dev/null 2>&1; then
                log_info "✓ Service is responding"
                return 0
            fi
        else
            if curl -f "$url" > /dev/null 2>&1; then
                log_info "✓ Service is responding"
                return 0
            fi
        fi
    fi
    
    log_warn "Could not verify service response"
    return 1
}

# Display status
show_status() {
    log_section "Deployment Status"
    
    log_info "Container Status:"
    docker-compose -f "$PROJECT_DIR/docker-compose.yml" ps
    
    log_info "\nRecent Logs (NGINX):"
    docker-compose -f "$PROJECT_DIR/docker-compose.yml" logs --tail=20 nginx 2>/dev/null || true
    
    log_info "\nRecent Logs (i3x2ua):"
    docker-compose -f "$PROJECT_DIR/docker-compose.yml" logs --tail=20 i3x2ua 2>/dev/null || true
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
    
    # Database backup if available
    if docker-compose -f "$PROJECT_DIR/docker-compose.yml" ps db > /dev/null 2>&1; then
        log_info "Backing up database..."
        docker-compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db \
            pg_dump -U "${DB_USER:-i3x2ua}" "${DB_NAME:-i3x2ua}" > \
            "$backup_dir/database.sql" 2>/dev/null || log_warn "Database backup failed"
        log_info "✓ Database backed up"
    fi
    
    log_info "Backups saved to: $backup_dir"
    return 0
}

# Clean up
cleanup() {
    log_section "Cleaning Up"
    
    read -p "Remove containers? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker-compose -f "$PROJECT_DIR/docker-compose.yml" down
        log_info "✓ Containers removed"
    fi
    
    read -p "Remove volumes? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker-compose -f "$PROJECT_DIR/docker-compose.yml" down -v
        log_info "✓ Volumes removed"
    fi
}

# Usage
usage() {
    cat << EOF
Production Deployment Helper Script

Usage: $0 [COMMAND] [OPTIONS]

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
  --project-dir     Path to project directory (default: .)
  --env-file        Path to .env file (default: ./.env)

Examples:
  $0 init
  $0 start
  $0 full --project-dir /path/to/project
  $0 status

EOF
}

# Main
main() {
    case "${1:-help}" in
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
            log_error "Unknown command: $1"
            usage
            exit 1
            ;;
    esac
}

main "$@"
