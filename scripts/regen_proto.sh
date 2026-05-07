#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m grpc_tools.protoc \
  -I saebooks/proto \
  --python_out=saebooks/grpc_gen \
  --grpc_python_out=saebooks/grpc_gen \
  saebooks/proto/saebooks.proto
# Fix bare import that grpc_tools generates for package usage
sed -i 's/^import saebooks_pb2/from saebooks.grpc_gen import saebooks_pb2/' \
  saebooks/grpc_gen/saebooks_pb2_grpc.py
echo "Proto stubs regenerated and import patched"
