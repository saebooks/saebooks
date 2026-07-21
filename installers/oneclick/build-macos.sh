#!/usr/bin/env bash
# Build the SAE Books one-click community server binary for macOS.
#
# Usage:  installers/oneclick/build-macos.sh [output-dir]
#
# Needs: python3.12 (brew install python@3.12), curl, tar.
# Output: dist/SAEBooks (single-file console binary), ad-hoc signed.
# NOT notarized — Gatekeeper warns on first run (right-click → Open).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_REPO="$(cd "$HERE/../.." && pwd)"
WEB_REPO="${SAEBOOKS_WEB_REPO:-$(cd "$ENGINE_REPO/../saebooks-web" && pwd)}"
OUT="${1:-$HERE/dist}"
BUILD_VENV="$HERE/.build-venv"
TOOLS="$HERE/.build-tools"
CONNECPY_VERSION=2.3.0
TAILWIND_VERSION=v3.4.17
PYTHON="${PYTHON:-python3.12}"

case "$(uname -m)" in
    arm64)  PLUGIN_ARCH=arm64; TW_ARCH=macos-arm64 ;;
    x86_64) PLUGIN_ARCH=amd64; TW_ARCH=macos-x64 ;;
    *) echo "unsupported arch" >&2; exit 1 ;;
esac

echo "== engine: $ENGINE_REPO"
echo "== web:    $WEB_REPO"

rm -rf "$BUILD_VENV"
"$PYTHON" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/pip" install -q --upgrade pip
"$BUILD_VENV/bin/pip" install -q pyinstaller grpcio-tools

mkdir -p "$TOOLS"

# --- protoc-gen-connecpy plugin ----------------------------------------------
if [ ! -x "$TOOLS/protoc-gen-connecpy" ]; then
    curl -fsSL -o "$TOOLS/connecpy.tgz" \
        "https://github.com/i2y/connecpy/releases/download/v${CONNECPY_VERSION}/protoc-gen-connecpy_${CONNECPY_VERSION}_darwin_${PLUGIN_ARCH}.tar.gz"
    tar -C "$TOOLS" -xzf "$TOOLS/connecpy.tgz" protoc-gen-connecpy
    chmod +x "$TOOLS/protoc-gen-connecpy"
    rm "$TOOLS/connecpy.tgz"
fi
export PATH="$TOOLS:$PATH"

# --- grpc stubs into the repo BEFORE the non-editable install ----------------
(
    cd "$ENGINE_REPO"
    mkdir -p saebooks/grpc_gen
    "$BUILD_VENV/bin/python" -m grpc_tools.protoc -I saebooks/proto \
        --python_out=saebooks/grpc_gen \
        --grpc_python_out=saebooks/grpc_gen \
        --connecpy_out=saebooks/grpc_gen \
        saebooks/proto/saebooks.proto
    touch saebooks/grpc_gen/__init__.py
    sed -i '' 's/^import saebooks_pb2/from saebooks.grpc_gen import saebooks_pb2/' \
        saebooks/grpc_gen/saebooks_pb2_grpc.py saebooks/grpc_gen/saebooks_connecpy.py
)

# Non-editable installs: PyInstaller cannot collect PEP 660 editable packages.
"$BUILD_VENV/bin/pip" install -q "$ENGINE_REPO" "$WEB_REPO"

# --- tailwind.css ------------------------------------------------------------
if [ ! -x "$TOOLS/tailwindcss" ]; then
    curl -fsSL -o "$TOOLS/tailwindcss" \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${TW_ARCH}"
    chmod +x "$TOOLS/tailwindcss"
fi
(
    cd "$WEB_REPO"
    "$TOOLS/tailwindcss" -c tailwind.config.js -i ./assets/tailwind.css -o static/tailwind.css --minify
)

# --- freeze ------------------------------------------------------------------
cd "$HERE"
SAEBOOKS_WEB_REPO="$WEB_REPO" "$BUILD_VENV/bin/pyinstaller" \
    --distpath "$OUT" --workpath "$HERE/.build" --noconfirm \
    saebooks-oneclick.spec

BIN="$OUT/SAEBooks"
codesign --force --sign - "$BIN"
echo
echo "== built: $BIN ($(du -h "$BIN" | cut -f1)) — ad-hoc signed, NOT notarized"
shasum -a 256 "$BIN"
