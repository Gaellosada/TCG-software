#!/usr/bin/env bash
# Build the TCG backend PyInstaller sidecar and stage it for Tauri's externalBin.
#
# Reproducible; run from anywhere. Requires the project's uv venv (with the
# `desktop` extra: `uv sync --extra desktop`) and a Rust toolchain (for the
# host target triple Tauri expects in the binary name).
#
#   desktop/sidecar/build_sidecar.sh
#
# Produces: desktop/src-tauri/binaries/tcg-backend-<target-triple>[.exe]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # desktop/sidecar
REPO="$(cd "$HERE/../.." && pwd)"                       # repo root

RUSTC="$(command -v rustc || true)"
[ -z "$RUSTC" ] && [ -x "$HOME/.cargo/bin/rustc" ] && RUSTC="$HOME/.cargo/bin/rustc"
[ -n "$RUSTC" ] || { echo "ERROR: rustc not found (install Rust)." >&2; exit 1; }
TRIPLE="$("$RUSTC" -Vv | sed -n 's/host: //p')"
[ -n "$TRIPLE" ] || { echo "ERROR: cannot determine Rust host triple." >&2; exit 1; }
EXT=""; case "$TRIPLE" in *windows*) EXT=".exe";; esac

echo "[sidecar] building one-file backend (triple=$TRIPLE) ..."
"$REPO/.venv/bin/pyinstaller" --noconfirm --clean \
  "$HERE/tcg-backend.spec" \
  --distpath "$HERE/dist" \
  --workpath "$HERE/build"

DEST="$HERE/../src-tauri/binaries"
mkdir -p "$DEST"
cp "$HERE/dist/tcg-backend$EXT" "$DEST/tcg-backend-$TRIPLE$EXT"
echo "[sidecar] staged -> $DEST/tcg-backend-$TRIPLE$EXT"
ls -la "$DEST"
