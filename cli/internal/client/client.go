package client

import (
	"fmt"
	"net/http"
	"os"

	"connectrpc.com/connect"

	"github.com/saebooks/saebooks/cli/gen/saebooksconnect"
	"github.com/saebooks/saebooks/cli/internal/config"
)

// tokenTransport injects the Authorization header on every request.
type tokenTransport struct {
	base  http.RoundTripper
	token string
}

func (t *tokenTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("Authorization", "Bearer "+t.token)
	return t.base.RoundTrip(req)
}

// Options for overriding at call site.
type Options struct {
	// ExplicitToken overrides profile token if set.
	ExplicitToken string
}

// New returns a SAEBooks Connect client for the given profile.
// Token resolution order:
//  1. opts.ExplicitToken (--token flag)
//  2. SAEBOOKS_TOKEN env var
//  3. profile.JWT
//  4. profile.APIToken
//  5. error
func New(profile config.Profile, opts Options) (saebooksconnect.SAEBooksClient, error) {
	token := resolveToken(profile, opts)
	if token == "" {
		return nil, fmt.Errorf("no auth token found — run `sae books auth login` or set SAEBOOKS_TOKEN")
	}

	transport := &tokenTransport{
		base:  http.DefaultTransport,
		token: token,
	}

	httpClient := &http.Client{Transport: transport}

	c := saebooksconnect.NewSAEBooksClient(
		httpClient,
		profile.Endpoint,
		connect.WithGRPCWeb(), // grpc-web works with plain HTTP/1.1 proxies
	)
	return c, nil
}

// NewUnauthenticated returns a client with no auth — used only for Heartbeat
// during initial auth-login where we validate the endpoint first.
func NewUnauthenticated(endpoint string) saebooksconnect.SAEBooksClient {
	return saebooksconnect.NewSAEBooksClient(http.DefaultClient, endpoint, connect.WithGRPCWeb())
}

func resolveToken(profile config.Profile, opts Options) string {
	if opts.ExplicitToken != "" {
		return opts.ExplicitToken
	}
	if env := os.Getenv("SAEBOOKS_TOKEN"); env != "" {
		return env
	}
	if profile.JWT != "" {
		return profile.JWT
	}
	return profile.APIToken
}
