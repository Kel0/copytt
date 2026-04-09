#!/usr/bin/env bash
# One-shot bring-up: build, start, wait for health, open the dashboard.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "no .env found — copy .env.example to .env and fill in your keys first:"
  echo "  cp .env.example .env"
  exit 1
fi

echo "→ building & starting container..."
docker compose up -d --build

URL="http://127.0.0.1:47821"
echo "→ waiting for $URL to come up..."
for i in $(seq 1 40); do
  if curl -fs -o /dev/null "$URL"; then
    echo "→ ready"
    break
  fi
  sleep 0.5
done

if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
else
  echo "open $URL in your browser"
fi

echo
echo "logs:    docker compose logs -f"
echo "stop:    docker compose down"
