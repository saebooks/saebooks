#!/usr/bin/env bash
# Regenerate the proto stubs in saebooks/grpc_gen/.
#
# Runs three plugins against saebooks/proto/saebooks.proto:
#   --python_out      → saebooks_pb2.py        (message classes)
#   --grpc_python_out → saebooks_pb2_grpc.py   (grpcio servicer + stub)
#   --connecpy_out    → saebooks_connecpy.py   (Connect ASGI / WSGI / client)
#
# protoc-gen-connecpy is a Go binary; install from
# https://github.com/i2y/connecpy/releases/. The image build does this
# automatically; for local dev, drop the binary into your $PATH and pin
# the version to match `connecpy` in pyproject.toml.
#
# Both grpc-tools and protoc-gen-connecpy generate bare ``import
# saebooks_pb2`` lines that don't work when grpc_gen/ is a package — we
# patch them in-place to the package-qualified form.
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/python -m grpc_tools.protoc \
  -I saebooks/proto \
  --python_out=saebooks/grpc_gen \
  --grpc_python_out=saebooks/grpc_gen \
  --connecpy_out=saebooks/grpc_gen \
  saebooks/proto/saebooks.proto

sed -i 's/^import saebooks_pb2/from saebooks.grpc_gen import saebooks_pb2/' \
  saebooks/grpc_gen/saebooks_pb2_grpc.py
sed -i 's/^import saebooks_pb2/from saebooks.grpc_gen import saebooks_pb2/' \
  saebooks/grpc_gen/saebooks_connecpy.py

echo "Proto stubs regenerated (pb2 + grpc + connecpy) and imports patched"
