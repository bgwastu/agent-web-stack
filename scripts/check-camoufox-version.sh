#!/usr/bin/env bash
# check-camoufox-version.sh
#
# Checks the latest Camoufox release on GitHub and compares with the current
# version in .env. If a newer version is found, updates .env and triggers a
# Docker rebuild so the stack always uses the latest Camoufox.
#
# Usage:
#   ./scripts/check-camoufox-version.sh          # check and update
#   ./scripts/check-camoufox-version.sh --dry-run # check only, no changes
#
# Designed to be run as a cron job:
#   0 6 * * * /path/to/agent-web-stack/scripts/check-camoufox-version.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
COMPOSE_FILE="$REPO_DIR/docker-compose.yml"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# ── 1. Fetch latest release from GitHub ──
echo "[camoufox] Checking latest release..."
LATEST_JSON=$(curl -sf "https://api.github.com/repos/daijro/camoufox/releases/latest" 2>/dev/null || true)

if [[ -z "$LATEST_JSON" ]]; then
  echo "[camoufox] WARNING: Could not fetch latest release (rate limited or offline). Skipping."
  exit 0
fi

LATEST_TAG=$(echo "$LATEST_JSON" | python3 -c "
import sys, json
try:
  data = json.load(sys.stdin)
  tag = data.get('tag_name', '')
  print(tag.lstrip('v'))
except: pass
" 2>/dev/null || true)

if [[ -z "$LATEST_TAG" ]]; then
  echo "[camoufox] WARNING: Could not parse latest tag. Skipping."
  exit 0
fi

# Parse version and release from tag (e.g. "135.0.1-beta.24")
if echo "$LATEST_TAG" | grep -q '-'; then
  LATEST_VERSION=$(echo "$LATEST_TAG" | cut -d- -f1)
  LATEST_RELEASE=$(echo "$LATEST_TAG" | cut -d- -f2)
else
  LATEST_VERSION="$LATEST_TAG"
  LATEST_RELEASE="stable"
fi

# ── 2. Read current version from .env ──
CURRENT_VERSION=""
CURRENT_RELEASE=""

if [[ -f "$ENV_FILE" ]]; then
  CURRENT_VERSION=$(grep -oP '^CAMOUFOX_VERSION=\K.*' "$ENV_FILE" 2>/dev/null || true)
  CURRENT_RELEASE=$(grep -oP '^CAMOUFOX_RELEASE=\K.*' "$ENV_FILE" 2>/dev/null || true)
fi

if [[ -z "$CURRENT_VERSION" ]]; then
  CURRENT_VERSION="unknown"
fi
if [[ -z "$CURRENT_RELEASE" ]]; then
  CURRENT_RELEASE="unknown"
fi

echo "[camoufox] Current:  v${CURRENT_VERSION}-${CURRENT_RELEASE}"
echo "[camoufox] Latest:   v${LATEST_VERSION}-${LATEST_RELEASE}"

# ── 3. Compare and update if newer ──
if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" && "$CURRENT_RELEASE" == "$LATEST_RELEASE" ]]; then
  echo "[camoufox] Already up to date."
  exit 0
fi

echo "[camoufox] New version available!"

if $DRY_RUN; then
  echo "[camoufox] (dry-run) Would update to v${LATEST_VERSION}-${LATEST_RELEASE} and rebuild."
  exit 0
fi

# Update .env
if [[ -f "$ENV_FILE" ]]; then
  if grep -q '^CAMOUFOX_VERSION=' "$ENV_FILE"; then
    sed -i "s/^CAMOUFOX_VERSION=.*/CAMOUFOX_VERSION=$LATEST_VERSION/" "$ENV_FILE"
  else
    echo "CAMOUFOX_VERSION=$LATEST_VERSION" >> "$ENV_FILE"
  fi
  if grep -q '^CAMOUFOX_RELEASE=' "$ENV_FILE"; then
    sed -i "s/^CAMOUFOX_RELEASE=.*/CAMOUFOX_RELEASE=$LATEST_RELEASE/" "$ENV_FILE"
  else
    echo "CAMOUFOX_RELEASE=$LATEST_RELEASE" >> "$ENV_FILE"
  fi
  echo "[camoufox] Updated .env"
else
  echo "[camoufox] Creating .env with latest version"
  cat > "$ENV_FILE" <<EOF
CAMOUFOX_VERSION=$LATEST_VERSION
CAMOUFOX_RELEASE=$LATEST_RELEASE
EOF
fi

# Rebuild and restart the browser
echo "[camoufox] Rebuilding camofox-browser image..."
cd "$REPO_DIR"
docker compose build camofox-browser 2>&1 | tail -5
echo "[camoufox] Restarting camofox-browser..."
docker compose up -d camofox-browser 2>&1

echo "[camoufox] Update complete: v${LATEST_VERSION}-${LATEST_RELEASE}"
