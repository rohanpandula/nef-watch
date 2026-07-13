#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SDK_DIR="${SDK_DIR:-}"
IMAGE="${IMAGE:-nef-watch:nikon-linux}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"
SDK_MANIFEST="$ROOT/docker/nikon-sdk-v1.46.sha256"
EXPECTED_SDK_FINGERPRINT="${EXPECTED_SDK_FINGERPRINT:-0e8aba70b296966c03c407c8dd77ddee5c073924b9fb38f988881c366a1fbc51}"
ALLOW_UNVALIDATED_SDK="${ALLOW_UNVALIDATED_SDK:-0}"

if [[ -z "$SDK_DIR" ]]; then
  echo "Set SDK_DIR to Nikon's 'Image SDK/Library/win' directory." >&2
  exit 2
fi

if [[ ! "$PUID" =~ ^[0-9]+$ || ! "$PGID" =~ ^[0-9]+$ ]]; then
  echo "PUID and PGID must be numeric." >&2
  exit 2
fi

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$@"
  else
    echo "A SHA-256 tool (sha256sum or shasum) is required." >&2
    return 127
  fi
}

runtime_manifest="$(
  cd "$SDK_DIR"
  while read -r _ required; do
    if [[ -f "$required" ]]; then
      sha256 "$required"
    else
      printf 'MISSING  %s\n' "$required"
    fi
  done < "$SDK_MANIFEST"
)"
sdk_fingerprint="$(printf '%s\n' "$runtime_manifest" | sha256 | awk '{print $1}')"

if [[ "$sdk_fingerprint" != "$EXPECTED_SDK_FINGERPRINT" ]]; then
  if [[ "$ALLOW_UNVALIDATED_SDK" != "1" ]]; then
    cat >&2 <<EOF
Nikon SDK runtime does not match the validated v1.46.0 manifest.
  expected: $EXPECTED_SDK_FINGERPRINT
  actual:   $sdk_fingerprint
Set ALLOW_UNVALIDATED_SDK=1 only if you intend to establish a new validation baseline.
EOF
    exit 2
  fi
  echo "WARNING: building with an unvalidated Nikon SDK fingerprint: $sdk_fingerprint" >&2
fi

echo "Nikon SDK v1.46.0 manifest fingerprint: $sdk_fingerprint"

source_manifest="$(
  cd "$ROOT"
  {
    find docker -type f ! -name '.env'
    printf '%s\n' \
      .dockerignore \
      requirements.txt \
      pyproject.toml \
      tool/nef_watch.py \
      tool/nef_render_win.cpp \
      tool/nef_render_wine.sh
  } | LC_ALL=C sort -u | while IFS= read -r file; do
    file_hash="$(sha256 "$file" | awk '{print $1}')"
    printf '%s  %s\n' "$file_hash" "$file"
  done
)"
source_fingerprint="$(printf '%s\n' "$source_manifest" | sha256 | awk '{print $1}')"
echo "nef-watch source fingerprint: $source_fingerprint"

if [[ "${VERIFY_SDK_ONLY:-0}" == "1" ]]; then
  exit 0
fi

exec docker build \
  --platform linux/amd64 \
  --build-arg "ALLOW_UNVALIDATED_SDK=$ALLOW_UNVALIDATED_SDK" \
  --build-arg "NEF_WATCH_UID=$PUID" \
  --build-arg "NEF_WATCH_GID=$PGID" \
  --build-arg "NEF_WATCH_SDK_FINGERPRINT=$sdk_fingerprint" \
  --build-arg "NEF_WATCH_SOURCE_FINGERPRINT=$source_fingerprint" \
  --build-context "nikon-sdk=$SDK_DIR" \
  --file "$ROOT/docker/Dockerfile" \
  --label "io.nef-watch.nikon-sdk.fingerprint=$sdk_fingerprint" \
  --tag "$IMAGE" \
  "$ROOT"
