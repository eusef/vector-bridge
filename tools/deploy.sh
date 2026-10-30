#!/usr/bin/env bash
# Rsync the repo to the Pi, run the installer as root, restart the service.
# Usage: tools/deploy.sh [user@host]   (default vectorbridge@vectorbridge.local)
set -euo pipefail
TARGET="${1:-vectorbridge@vectorbridge.local}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude 'tools/.venv' \
  --exclude '__pycache__' --exclude '*.egg-info' --exclude '.pytest_cache' \
  "$REPO/" "$TARGET:~/vector-bridge/"

ssh -t "$TARGET" 'sudo bash ~/vector-bridge/scripts/install.sh && sudo systemctl restart vector-bridge vector-bridge-tuner && sleep 2 && systemctl is-active vector-bridge vector-bridge-tuner'
