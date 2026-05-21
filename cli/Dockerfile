# Multi-stage build for the SAE Books CLI.
# Produces a minimal final image with just the binary.

# ---- build stage ----
FROM golang:1.22 AS builder

ARG TARGETOS=linux
ARG TARGETARCH=amd64
ARG VERSION=dev
ARG COMMIT=none
ARG DATE=unknown

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build \
      -ldflags="-s -w \
        -X github.com/saebooks/saebooks/cli/cmd.Version=${VERSION} \
        -X github.com/saebooks/saebooks/cli/cmd.Commit=${COMMIT} \
        -X 'github.com/saebooks/saebooks/cli/cmd.Date=${DATE}'" \
      -o /dist/sae .

# ---- final stage ----
FROM gcr.io/distroless/static:nonroot

COPY --from=builder /dist/sae /usr/local/bin/sae

ENTRYPOINT ["/usr/local/bin/sae"]
CMD ["--help"]
