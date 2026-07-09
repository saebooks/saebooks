# grpcwebproxy — wraps the official improbable-eng release binary so
# it can run as a sidecar to the saebooks api. Static Linux binary,
# no real build needed — just unzip + chmod.
#
# Source: https://github.com/improbable-eng/grpc-web/releases
FROM debian:bookworm-slim AS fetcher

ARG GRPCWEBPROXY_VERSION=v0.15.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/proxy.zip \
        "https://github.com/improbable-eng/grpc-web/releases/download/${GRPCWEBPROXY_VERSION}/grpcwebproxy-${GRPCWEBPROXY_VERSION}-linux-x86_64.zip" \
    && unzip /tmp/proxy.zip -d /tmp \
    && mv /tmp/dist/grpcwebproxy-${GRPCWEBPROXY_VERSION}-linux-x86_64 /usr/local/bin/grpcwebproxy \
    && chmod +x /usr/local/bin/grpcwebproxy

# Minimal runtime — gcr.io/distroless/base would be nicer but
# debian-slim is what the rest of the deployment stack uses.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --shell /usr/sbin/nologin grpcwebproxy

COPY --from=fetcher /usr/local/bin/grpcwebproxy /usr/local/bin/grpcwebproxy

USER grpcwebproxy

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/grpcwebproxy"]
