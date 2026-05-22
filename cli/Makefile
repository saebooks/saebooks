MODULE    := github.com/saebooks/saebooks/cli
VERSION   ?= 0.1.0
COMMIT    ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
DATE      ?= $(shell date -u +%Y-%m-%dT%H:%M:%SZ)
LDFLAGS   := -s -w \
             -X $(MODULE)/cmd.Version=$(VERSION) \
             -X $(MODULE)/cmd.Commit=$(COMMIT) \
             -X '$(MODULE)/cmd.Date=$(DATE)'

GO_DOCKER := docker run --rm -v $(CURDIR):/work -w /work golang:1.22
BUF       := docker run --rm -v $(CURDIR):/work -w /work bufbuild/buf:1.40.0

.PHONY: all build generate tidy vet test install clean dist

all: generate tidy build

## generate — run buf codegen (requires docker)
generate:
	$(BUF) generate
	@# Move pb.go to correct package subdirectory if needed
	@if [ -f gen/saebooks.pb.go ] && [ ! -f gen/saebooks/saebooks.pb.go ]; then \
		mkdir -p gen/saebooks && mv gen/saebooks.pb.go gen/saebooks/saebooks.pb.go; \
	fi

## tidy — go mod tidy inside docker
tidy:
	$(GO_DOCKER) go mod tidy

## build — build for the host OS/arch
build:
	$(GO_DOCKER) go build -ldflags="$(LDFLAGS)" -o dist/sae-books .

## vet — run go vet
vet:
	$(GO_DOCKER) go vet ./...

## test — run tests (none yet)
test:
	$(GO_DOCKER) go test ./...

## dist — cross-compile for linux/amd64 and darwin/arm64
dist: dist/sae-books-linux-amd64 dist/sae-books-darwin-arm64

dist/sae-books-linux-amd64:
	docker run --rm -v $(CURDIR):/work -w /work \
	  -e GOOS=linux -e GOARCH=amd64 \
	  golang:1.22 go build -ldflags="$(LDFLAGS)" -o dist/sae-books-linux-amd64 .

dist/sae-books-darwin-arm64:
	docker run --rm -v $(CURDIR):/work -w /work \
	  -e GOOS=darwin -e GOARCH=arm64 \
	  golang:1.22 go build -ldflags="$(LDFLAGS)" -o dist/sae-books-darwin-arm64 .

## install — install to /usr/local/bin/sae (requires root or sudo)
install: dist/sae-books-linux-amd64
	install -m 755 dist/sae-books-linux-amd64 /usr/local/bin/sae

## clean — remove dist/ and generated code
clean:
	rm -rf dist/ gen/

help:
	@grep -E '^## ' Makefile | sed 's/## //'
