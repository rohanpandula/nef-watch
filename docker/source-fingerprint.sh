#!/usr/bin/env bash
# Print the canonical fingerprint of every source file that can affect the
# public Linux image. This intentionally needs neither Docker nor Nikon's SDK.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

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

source_manifest="$({
  cd "$ROOT"
  {
    find docker -type d -name '__pycache__' -prune -o \
      -type f \
      ! -name '.env' \
      ! -name '.DS_Store' \
      ! -name '*.py[co]' \
      -print
    printf '%s\n' \
      .dockerignore \
      requirements.txt \
      pyproject.toml \
      tool/nef_watch.py \
      tool/nef_render_win.cpp \
      tool/nef_render_wine.sh
  } | LC_ALL=C sort -u | while IFS= read -r file; do
    if [[ ! -f "$file" ]]; then
      echo "Missing fingerprint input: $file" >&2
      exit 2
    fi
    file_hash="$(sha256 "$file" | awk '{print $1}')"
    printf '%s  %s\n' "$file_hash" "$file"
  done
})"

printf '%s\n' "$source_manifest" | sha256 | awk '{print $1}'
