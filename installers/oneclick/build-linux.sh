#!/usr/bin/env bash
# Build the SAE Books one-click community server binary for Linux.
#
# Usage:  installers/oneclick/build-linux.sh [output-dir]
#
# Needs: uv on PATH, network access (first run fetches Python 3.12 +
# wheels + the protoc-gen-connecpy plugin binary).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENGINE_REPO="$(cd "$HERE/../.." && pwd)"
WEB_REPO="${SAEBOOKS_WEB_REPO:-$(cd "$ENGINE_REPO/../saebooks-web" && pwd)}"
OUT="${1:-$HERE/dist}"
BUILD_VENV="$HERE/.build-venv"
CONNECPY_VERSION=2.3.0

echo "== engine: $ENGINE_REPO"
echo "== web:    $WEB_REPO"

UV_VENV_CLEAR=1 uv venv --python 3.12 "$BUILD_VENV"
VIRTUAL_ENV="$BUILD_VENV" uv pip install -q pyinstaller grpcio-tools

# --- grpc stubs: generate into the repo BEFORE installing the engine, so the
# --- non-editable site-packages copy ships them --------------------------------
if ! command -v protoc-gen-connecpy >/dev/null; then
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64) PLUGIN_ARCH=amd64 ;;
        aarch64) PLUGIN_ARCH=arm64 ;;
        *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
    esac
    mkdir -p "$HOME/.local/bin"
    curl -fsSL -o /tmp/protoc-gen-connecpy.tgz \
        "https://github.com/i2y/connecpy/releases/download/v${CONNECPY_VERSION}/protoc-gen-connecpy_${CONNECPY_VERSION}_linux_${PLUGIN_ARCH}.tar.gz"
    tar -C "$HOME/.local/bin" -xzf /tmp/protoc-gen-connecpy.tgz protoc-gen-connecpy
    chmod +x "$HOME/.local/bin/protoc-gen-connecpy"
    rm /tmp/protoc-gen-connecpy.tgz
    export PATH="$HOME/.local/bin:$PATH"
fi
(
    cd "$ENGINE_REPO"
    mkdir -p saebooks/grpc_gen
    "$BUILD_VENV/bin/python" -m grpc_tools.protoc -I saebooks/proto \
        --python_out=saebooks/grpc_gen \
        --grpc_python_out=saebooks/grpc_gen \
        --connecpy_out=saebooks/grpc_gen \
        saebooks/proto/saebooks.proto
    touch saebooks/grpc_gen/__init__.py
    sed -i 's/^import saebooks_pb2/from saebooks.grpc_gen import saebooks_pb2/' \
        saebooks/grpc_gen/saebooks_pb2_grpc.py saebooks/grpc_gen/saebooks_connecpy.py
)

# Non-editable installs: PyInstaller cannot collect packages exposed through
# PEP 660 editable-install import hooks.
VIRTUAL_ENV="$BUILD_VENV" uv pip install -q "$ENGINE_REPO" "$WEB_REPO"

# --- tailwind.css: built at docker-image build time normally (Dockerfile
# --- stage 0); the standalone binary reproduces it for the bundle ------------
TAILWIND_VERSION=v3.4.17
if ! command -v tailwindcss >/dev/null; then
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64) TW_ARCH=linux-x64 ;;
        aarch64) TW_ARCH=linux-arm64 ;;
        *) echo "unsupported arch for tailwindcss: $ARCH" >&2; exit 1 ;;
    esac
    curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-${TW_ARCH}" \
        -o "$HOME/.local/bin/tailwindcss"
    chmod +x "$HOME/.local/bin/tailwindcss"
fi
(
    cd "$WEB_REPO"
    tailwindcss -c tailwind.config.js -i ./assets/tailwind.css -o static/tailwind.css --minify
)

# --- freeze ------------------------------------------------------------------
cd "$HERE"
SAEBOOKS_WEB_REPO="$WEB_REPO" "$BUILD_VENV/bin/pyinstaller" \
    --distpath "$OUT" --workpath "$HERE/.build" --noconfirm \
    saebooks-oneclick.spec

BIN="$OUT/SAEBooks"
echo
echo "== built: $BIN ($(du -h "$BIN" | cut -f1))"
sha256sum "$BIN"
