# SAE Books CLI

A command-line interface to the [SAE Books](https://saebooks.com.au) accounting backend.
Communicates over Connect-RPC (grpc-web compatible — works through any HTTP proxy).

## Quickstart

### 1. Install

**Linux/amd64 (from pre-built binary):**
```bash
sudo install -m 755 dist/sae-books-linux-amd64 /usr/local/bin/sae
```

**macOS/arm64:**
```bash
sudo install -m 755 dist/sae-books-darwin-arm64 /usr/local/bin/sae
```

**Build from source (requires Docker):**
```bash
make dist
```

### 2. Configure a profile

The CLI stores profiles in `~/.config/saebooks/config.toml`.

Create the file manually, or let `auth login` create it for you:

```toml
default_profile = "local"

[profiles.local]
endpoint = "http://localhost:18310"
api_token = "saebk_..."
output = "table"

[profiles.prod]
endpoint = "https://api.saebooks.com.au"
api_token = "saebk_..."
```

Alternatively, export `SAEBOOKS_PROFILE` to switch profiles without editing the file.

### 3. Log in

```bash
# First time — provide the endpoint, paste your token when prompted
sae auth login --endpoint http://localhost:18310

# Subsequent logins (endpoint remembered from profile)
sae auth login
```

The login command accepts either a raw JWT or an `saebk_*` API token.
It probes the backend with a Heartbeat call before saving.

### 4. Verify authentication

```bash
sae auth whoami
```

---

## Command reference

```
sae auth login                          # log in / store credentials
sae auth whoami                         # check auth + server status
sae auth token create --name "CI"       # create API token (stub — needs backend)
sae auth token list                     # list API tokens     (stub — needs backend)
sae auth token revoke <id>              # revoke API token    (stub — needs backend)

sae invoice list                        # list invoices
sae invoice list --status DRAFT         # filter by status
sae invoice get <id>                    # get invoice by ID

sae customer list                       # list customers (contacts)
sae customer list --search "Acme"       # search by name/email
sae customer get <id>                   # get customer by ID
sae customer create --name "Acme Corp" --email "ap@acme.com"

sae vendor list                         # list vendors (same Contact endpoints)
sae vendor get <id>
sae vendor create --name "Supplier Ltd"

sae bill list                           # list bills (AP)
sae bill list --status UNPAID
sae bill get <id>

sae payment list                        # list payments
sae payment list --direction INCOMING
sae payment get <id>

sae je list                             # list journal entries
sae je list --status POSTED
sae je get <id>
```

### Global flags

| Flag | Short | Description |
|------|-------|-------------|
| `--profile <name>` | | Config profile (overrides `SAEBOOKS_PROFILE`) |
| `--output <fmt>` | `-o` | `table` \| `json` \| `yaml` |
| `--compact` | | Compact JSON (no pretty-print) |
| `--token <tok>` | | Explicit token (overrides profile + env) |

### Environment variables

| Variable | Description |
|----------|-------------|
| `SAEBOOKS_PROFILE` | Active profile name |
| `SAEBOOKS_TOKEN` | Auth token (overrides profile) |

---

## Output formats

- **table** — human-readable aligned table (default when stdout is a TTY)
- **json** — pretty-printed JSON (default when stdout is not a TTY)
- **yaml** — YAML

```bash
# Pipe-friendly — auto-switches to JSON
sae invoice list | jq '.[].status'

# Force JSON
sae invoice list -o json

# Compact JSON for scripts
sae invoice list -o json --compact
```

---

## Building

Requires Docker (no local Go needed).

```bash
make generate   # regenerate protobuf + connect code from proto/saebooks.proto
make tidy       # go mod tidy
make build      # build for host arch
make dist       # cross-compile linux/amd64 + darwin/arm64
make vet        # go vet
make test       # go test (no tests yet)
make install    # install to /usr/local/bin/sae
```

---

## Stubs (waiting on backend track)

The following commands are present but exit with code 2 ("stub") until the backend
provides the corresponding proto definitions:

- `sae auth token create` — needs `ApiTokens.CreateApiToken` RPC
- `sae auth token list` — needs `ApiTokens.ListApiTokens` RPC
- `sae auth token revoke` — needs `ApiTokens.RevokeApiToken` RPC
- `sae invoice create` — needs `CreateInvoiceRequest` + RPC in proto
- `sae invoice send` — needs `SendInvoice` RPC

See `SUMMARY.md` for the full list of what the backend track needs to provide.
