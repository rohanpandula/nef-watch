#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-nef-watch:linux-amd64}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"

if [[ ! "$PUID" =~ ^[0-9]+$ || ! "$PGID" =~ ^[0-9]+$ ]]; then
  echo "PUID and PGID must be numeric." >&2
  exit 2
fi

source_fingerprint="$(bash "$ROOT/docker/source-fingerprint.sh" "$ROOT")"
echo "nef-watch source fingerprint: $source_fingerprint"

if [[ "${VERIFY_SOURCE_ONLY:-0}" == "1" ]]; then
  exit 0
fi

exec docker build \
  --platform linux/amd64 \
  --build-arg "NEF_WATCH_UID=$PUID" \
  --build-arg "NEF_WATCH_GID=$PGID" \
  --build-arg "NEF_WATCH_SOURCE_FINGERPRINT=$source_fingerprint" \
  --file "$ROOT/docker/Dockerfile" \
  --label "io.nef-watch.source.fingerprint=$source_fingerprint" \
  --tag "$IMAGE" \
  "$ROOT"
