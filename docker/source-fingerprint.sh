#!/usr/bin/env bash
# Print the canonical fingerprint of every source file that can affect the
# public Linux image. This intentionally needs neither Docker nor Nikon's SDK.
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

exec python3 -I "$ROOT/docker/source_fingerprint.py" --root "$ROOT"
