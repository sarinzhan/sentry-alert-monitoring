#!/usr/bin/env bash
# Deploy on the server: pull the latest changes from the remote repository and
# restart the compose stack with a rebuild (sentry-telegram + sentry-web).
#
# Usage (on the server, from any directory):
#   bash /opt/docker/sentry-alert-monitoring/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== git pull =="
git pull --ff-only

# docker compose v2 (plugin) with a fallback to the standalone v1 binary
if docker compose version >/dev/null 2>&1; then
  dc() { docker compose "$@"; }
else
  dc() { docker-compose "$@"; }
fi

echo "== rebuild + restart =="
dc up -d --build --remove-orphans

# rebuilds leave the previous images dangling — reclaim the space
echo "== cleanup old images =="
docker image prune -f

echo "== status =="
dc ps

echo "== health =="
sleep 3
if curl -fsS http://localhost:8080/health >/dev/null; then
  echo "sentry-telegram: ok"
else
  echo "sentry-telegram health check FAILED — check: docker logs sentry-telegram"
  exit 1
fi
if curl -fsS -o /dev/null http://localhost:8081/admin-web/; then
  echo "sentry-web: ok"
else
  echo "sentry-web check FAILED — check: docker logs sentry-web"
  exit 1
fi
