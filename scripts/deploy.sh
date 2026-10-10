#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

git fetch --quiet origin main
git reset --hard "${1:-origin/main}"

docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null

for _ in $(seq 1 30); do
  if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" 2>/dev/null; then
    echo "deployed $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done

docker compose logs --tail 80 app
exit 1
