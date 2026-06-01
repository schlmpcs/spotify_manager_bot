#!/usr/bin/env bash
#
# Quick deploy for the Spotify Manager Automatization bot.
# Run on the server, from the repo root:  bash deploy.sh
#
# What it does: pull latest main -> rebuild the image -> recreate the container
# -> prune dangling images -> tail logs. Safe to re-run.

set -euo pipefail

cd "$(dirname "$0")"

# docker-compose v1 vs the v2 plugin ("docker compose").
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: neither 'docker compose' nor 'docker-compose' is installed." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

echo "==> Pulling latest code"
git fetch --quiet origin
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull --ff-only origin "$BRANCH"

echo "==> Building image"
$COMPOSE build

echo "==> Restarting container"
$COMPOSE up -d

echo "==> Cleaning up dangling images"
docker image prune -f >/dev/null

echo "==> Done. Tailing logs (Ctrl-C to stop — the bot keeps running)"
$COMPOSE logs -f --tail=50
