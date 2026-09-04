#!/bin/bash
# Upgrade a running Securo deployment from your own fork — data-safe by design.
#
# All persistent state lives in named compose volumes (pgdata, attachments,
# agent_knowledge, agent_embedding_models). This script never runs "down -v",
# never prunes volumes, and recreates containers only. If you delete it
# yourself (docker compose down -v, docker volume prune), no script can help.
#
# What it does, aborting at the first failure:
#   1. Backs the database up to backups/ (only while the stack is running)
#   2. Points git origin at the fork, checks out / ff-pulls the branch
#   3. Writes docker-compose.override.yml so backend + frontend build from
#      this clone's source instead of pulling the stock ghcr images
#   4. Builds, then recreates containers (volumes detach and reattach)
#   5. Waits for /api/health, then shows migration + health log lines
#
# Usage (from anywhere; run ON the machine where the stack runs):
#   scripts/upgrade-fork.sh [branch]      default branch: feature/amazon-order-matching

set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="${1:-feature/amazon-order-matching}"
FORK_URL="https://github.com/jfslovacek/securo.git"
PROD_FILE="docker-compose.prod.yml"
OVERRIDE_FILE="docker-compose.override.yml"
HEALTH_URL="http://localhost:${BACKEND_PORT:-8000}/api/health"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[ -f "$PROD_FILE" ] || error "Run this from the Securo clone root (no $PROD_FILE here)."

# ── Compose command (same detection order as install.sh) ───────────────────
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
elif command -v podman-compose >/dev/null 2>&1; then
  COMPOSE=(podman-compose)
else
  error "No working 'docker compose' / 'docker-compose' / 'podman-compose' found. Is Docker (or Podman) running?"
fi
# CLI existing ≠ daemon reachable: a stopped Docker Desktop passes the check
# above, then every docker call below fails with a raw socket error instead.
"${COMPOSE[@]}" -f "$PROD_FILE" ps >/dev/null 2>&1 \
  || error "Compose daemon unreachable — start Docker (Desktop) or Podman, then rerun."
compose() { "${COMPOSE[@]}" -f "$PROD_FILE" -f "$OVERRIDE_FILE" "$@"; }

# ── 1. Database backup (the stack must be up for this) ─────────────────────
mkdir -p backups
if "${COMPOSE[@]}" -f "$PROD_FILE" ps --status running db >/dev/null 2>&1 \
   && [ -n "$("${COMPOSE[@]}" -f "$PROD_FILE" ps -q db 2>/dev/null || true)" ]; then
  DUMP="backups/securo-pre-upgrade-$(date +%Y%m%d-%H%M%S).sql"
  info "Backing up the database to $DUMP …"
  "${COMPOSE[@]}" -f "$PROD_FILE" exec -T db pg_dump -U postgres securo > "$DUMP" \
    || error "pg_dump failed — aborting before touching anything."
  success "Backup written ($(du -h "$DUMP" | cut -f1)). Restore: cat $DUMP | ${COMPOSE[*]} -f $PROD_FILE exec -T db psql -U postgres securo"
else
  warn "Stack not running — skipping the database backup."
fi

# ── 2. Code: track the fork branch, never merge blindly ───────────────────
ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
if [ "$ORIGIN_URL" != "$FORK_URL" ]; then
  warn "origin points at ${ORIGIN_URL:-<nothing>} — repointing it at $FORK_URL"
  git remote set-url origin "$FORK_URL"
fi
info "Fetching origin and switching to '$BRANCH' …"
git fetch origin
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH" \
    || error "Local branch has commits not on origin/$BRANCH — push or rebase them first."
elif git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  git checkout -b "$BRANCH" "origin/$BRANCH"
else
  error "Branch '$BRANCH' not found locally or on origin. Typo? (git ls-remote --heads origin)"
fi
success "On $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)."

# ── 3. Compose override: build from source, not stock ghcr images ─────────
# Without this, prod compose pulls ghcr.io/securo-finance/* — which never
# contains fork-branch code. One backend build tags the image shared by
# backend, celery-worker, celery-beat and mcp-server. (Override is gitignored.)
if [ ! -f "$OVERRIDE_FILE" ]; then
  info "Writing $OVERRIDE_FILE (build backend + frontend from source) …"
  cat > "$OVERRIDE_FILE" <<'EOF'
services:
  backend:
    build:
      context: ./backend
  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
EOF
fi

# ── 4. Build + recreate (volumes are never removed here) ──────────────────
info "Building images from this clone's source (cached layers make reruns fast) …"
compose build
info "Recreating containers — named volumes detach and reattach untouched …"
compose up -d

# ── 5. Health gate ─────────────────────────────────────────────────────────
info "Waiting for $HEALTH_URL …"
for _ in $(seq 1 60); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    success "Backend is healthy."
    "${COMPOSE[@]}" -f "$PROD_FILE" logs --tail=40 backend 2>/dev/null | grep -iE "alembic|Running upgrade" | tail -3 || true
    echo
    success "Upgrade done. UI: http://localhost:${FRONTEND_PORT:-3000} — Import page should show the Purchases tab."
    exit 0
  fi
  sleep 1
done

warn "Backend did not answer $HEALTH_URL within 60s."
warn "Your data is intact — containers were recreated, volumes untouched."
warn "Inspect: ${COMPOSE[*]} -f $PROD_FILE logs backend | tail -50   (migrations run before the API starts)"
warn "Roll back: git checkout main && rerun this script."
exit 1
