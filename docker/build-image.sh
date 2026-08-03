#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-nef-watch:linux-amd64}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"

export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
unset GIT_DIR GIT_WORK_TREE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES

if [[ ! "$PUID" =~ ^[0-9]+$ || ! "$PGID" =~ ^[0-9]+$ ]]; then
  echo "PUID and PGID must be numeric." >&2
  exit 2
fi

source_revision="${NEF_WATCH_SOURCE_REVISION:-$(git -C "$ROOT" rev-parse --verify HEAD)}"
if [[ ! "$source_revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo "NEF_WATCH_SOURCE_REVISION must be an exact 40-character Git commit." >&2
  exit 2
fi
source_fingerprint="$(bash "$ROOT/docker/source-fingerprint.sh" "$ROOT")"
commit_fingerprint="$(python3 -I "$ROOT/docker/source_fingerprint.py" \
  --repo "$ROOT" --git-commit "$source_revision")"
if [[ "$source_fingerprint" != "$commit_fingerprint" ]]; then
  echo "Refusing to label a build from modified or untracked image inputs." >&2
  echo "Checked-out source fingerprint: $source_fingerprint" >&2
  echo "Labeled commit fingerprint:     $commit_fingerprint" >&2
  echo "Build the exact reviewed Git archive described in docs/DOCKER.md." >&2
  exit 2
fi
echo "nef-watch source fingerprint: $source_fingerprint"
echo "nef-watch source revision: $source_revision"

if [[ "${VERIFY_SOURCE_ONLY:-0}" == "1" ]]; then
  exit 0
fi

exec docker build \
  --platform linux/amd64 \
  --build-arg "NEF_WATCH_UID=$PUID" \
  --build-arg "NEF_WATCH_GID=$PGID" \
  --build-arg "NEF_WATCH_SOURCE_FINGERPRINT=$source_fingerprint" \
  --build-arg "NEF_WATCH_SOURCE_REVISION=$source_revision" \
  --file "$ROOT/docker/Dockerfile" \
  --label "io.nef-watch.source.fingerprint=$source_fingerprint" \
  --label "org.opencontainers.image.revision=$source_revision" \
  --tag "$IMAGE" \
  "$ROOT"
