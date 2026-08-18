#!/usr/bin/env bash
set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "Docker access is required. Run this as a user in the docker group (or with sudo)." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Create .env from .env.example and set a strong Grafana password first." >&2
  exit 1
fi

docker compose pull
docker compose up -d
docker compose ps
