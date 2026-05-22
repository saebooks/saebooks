package auth

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strings"

	pb "github.com/saebooks/saebooks/cli/gen/saebooks"
	"github.com/saebooks/saebooks/cli/internal/client"
	"github.com/saebooks/saebooks/cli/internal/config"
	"connectrpc.com/connect"
)

// Login performs the interactive login flow for a profile.
// Currently this is a "paste your JWT" flow; real device-flow is a TODO.
func Login(profileName, endpoint string, cfg *config.Config) error {
	fmt.Printf("Connecting to: %s\n", endpoint)
	fmt.Println()
	fmt.Println("Paste your API token or JWT below (it will not echo):")
	fmt.Print("> ")

	reader := bufio.NewReader(os.Stdin)
	token, err := reader.ReadString('\n')
	if err != nil {
		return fmt.Errorf("could not read token: %w", err)
	}
	token = strings.TrimSpace(token)
	if token == "" {
		return fmt.Errorf("no token provided")
	}

	// Probe the endpoint.
	p := config.Profile{
		Endpoint: endpoint,
		Output:   "table",
	}
	// Determine token type by prefix.
	if strings.HasPrefix(token, "saebk_") {
		p.APIToken = token
	} else {
		p.JWT = token
	}

	c, err := client.New(p, client.Options{})
	if err != nil {
		return err
	}

	resp, err := c.Heartbeat(context.Background(), connect.NewRequest(&pb.HeartbeatRequest{}))
	if err != nil {
		return fmt.Errorf("couldn't reach %s: %w", endpoint, err)
	}

	fmt.Printf("\nAuthenticated. Server status: %s\n", resp.Msg.Status)
	if resp.Msg.FreshJwt != "" && !strings.HasPrefix(token, "saebk_") {
		p.JWT = resp.Msg.FreshJwt
		fmt.Println("JWT refreshed and stored.")
	}

	if err := cfg.SetProfile(profileName, p); err != nil {
		return fmt.Errorf("could not save profile: %w", err)
	}
	fmt.Printf("Profile %q saved to %s\n", profileName, cfg.Path())
	return nil
}

// TODO: Device-flow PKCE / OAuth2 login will replace Login() once the backend
// provides an OAuth2 device-code endpoint.
