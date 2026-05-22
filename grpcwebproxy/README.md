# grpcwebproxy

Sidecar that fronts the saebooks api container's `:50051` gRPC server
with a [grpc-web](https://github.com/grpc/grpc-web) wire-format
adapter. Lets browsers and `connect.WithGRPCWeb()` clients talk to
the same proto schema over plain HTTP/1.1.

## Source

Wraps the official [improbable-eng/grpc-web release
binary](https://github.com/improbable-eng/grpc-web/releases) — pinned
to `v0.15.0`. The `Dockerfile` is a single-stage download + chmod,
no Go toolchain required.

## Deployment

Add to `bosun/compose/saebooks/docker-compose.yml` (host-local, not
committed):

```yaml
grpcwebproxy:
  build:
    context: /home/sauer/projects/saebooks/grpcwebproxy
  image: saebooks-grpcwebproxy:0.15.0
  restart: unless-stopped
  ports:
  - 10.0.2.1:18315:8080
  command:
  - --backend_addr=api:50051
  - --backend_tls=false
  - --run_tls_server=false
  - --server_http_debug_port=8080
  - --allow_all_origins
  - --use_websockets
  depends_on:
  - api
  networks:
  - default
```

## Auth

There is no auth surface on the proxy itself. `Authorization`
headers pass through transparently to the api's `:50051` gRPC
server, where the `BearerAuthInterceptor` in
`saebooks/grpc_server.py` enforces JWT / `saebk_*` / dev-token
verification — identical logic to the REST + Connect paths.

## Verify

```bash
# Inside any container on the saebooks_default network:
curl -sS -X POST \
  -H "Content-Type: application/grpc-web+proto" \
  -H "Authorization: Bearer saebk_<your-token>" \
  --data-binary @<framed-request> \
  http://grpcwebproxy:8080/saebooks.SAEBooks/Heartbeat

# Response headers will carry grpc-status: 0 on success, 16 on
# UNAUTHENTICATED, etc.
```
