package cmd

import (
	"context"
	"fmt"
	"os"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"

	pb "github.com/saebooks/saebooks/cli/gen/saebooks"
	"github.com/saebooks/saebooks/cli/internal/auth"
	"github.com/saebooks/saebooks/cli/internal/config"
	"github.com/saebooks/saebooks/cli/internal/output"
)

var authCmd = &cobra.Command{
	Use:   "auth",
	Short: "Authenticate and manage API tokens",
	Long: `Authentication commands for SAE Books.

Manage login sessions and API tokens.

Examples:
  sae books auth login --endpoint http://localhost:18310
  sae books auth whoami
  sae books auth token create --name "CI pipeline"
  sae books auth token list
  sae books auth token revoke <token-id>
`,
}

// ------ auth login ------

var authLoginEndpoint string

var authLoginCmd = &cobra.Command{
	Use:   "login",
	Short: "Log in to a SAE Books instance",
	Long: `Log in to a SAE Books instance.

Prompts for a JWT or API token and probes the endpoint with a Heartbeat call.
Saves the credentials under the active profile in ~/.config/saebooks/config.toml.

The --endpoint flag is required on first use for a profile.

Examples:
  sae books auth login --endpoint http://localhost:18310
  sae books auth login --endpoint https://api.saebooks.com.au --profile prod
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg, err := config.Load()
		if err != nil {
			return err
		}
		profileName := cfg.ActiveProfileName(profileFlag)

		// If endpoint not provided, try existing profile endpoint.
		endpoint := authLoginEndpoint
		if endpoint == "" {
			if p, ok := cfg.Profiles[profileName]; ok && p.Endpoint != "" {
				endpoint = p.Endpoint
			} else {
				return fmt.Errorf("--endpoint is required for a new profile")
			}
		}

		return auth.Login(profileName, endpoint, cfg)
	},
}

// ------ auth whoami ------

var authWhoamiCmd = &cobra.Command{
	Use:   "whoami",
	Short: "Show the authenticated user and server status",
	Long: `Call Heartbeat to verify authentication and show the server's response.

Examples:
  sae books auth whoami
  sae books auth whoami --profile prod
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		profileName := cfg.ActiveProfileName(profileFlag)
		prof, _ := cfg.ActiveProfile(profileName)

		resp, err := c.Heartbeat(context.Background(), connect.NewRequest(&pb.HeartbeatRequest{}))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}

		type whoamiOut struct {
			Status   string `json:"status" yaml:"status"`
			Endpoint string `json:"endpoint" yaml:"endpoint"`
			Profile  string `json:"profile" yaml:"profile"`
		}
		data := whoamiOut{
			Status:   resp.Msg.Status,
			Endpoint: prof.Endpoint,
			Profile:  profileName,
		}

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(data)
		case output.FormatYAML:
			return printer.PrintYAML(data)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"FIELD", "VALUE"})
			t.AppendRows([]output.Row{
				{"Profile", data.Profile},
				{"Endpoint", data.Endpoint},
				{"Status", data.Status},
			})
			output.PrintTable(t)
		}
		return nil
	},
}

// ------ auth token ------

var authTokenCmd = &cobra.Command{
	Use:   "token",
	Short: "Manage API tokens",
}

var (
	tokenCreateName   string
	tokenCreateScopes string
)

var authTokenCreateCmd = &cobra.Command{
	Use:   "create",
	Short: "Create a new API token",
	Long: `Create a new API token via the ApiTokens service.

The token is shown ONCE — save it immediately. The token value is not stored
locally (only the prefix is recorded for reference).

NOTE: Requires the backend track to provide the ApiTokens service in the proto.
This command is stubbed until that lands.

Examples:
  sae books auth token create --name "CI pipeline"
  sae books auth token create --name "Mobile app" --scope "read:invoices,read:bills"
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		// TODO: Call ApiTokens.CreateApiToken once the backend track adds that
		// service to the proto. The SAEBooks service does not yet expose token
		// management RPCs.
		fmt.Fprintln(os.Stderr, "stub: ApiTokens service not yet in proto — waiting on backend track")
		fmt.Fprintf(os.Stderr, "  Would create token: name=%q scopes=%q\n", tokenCreateName, tokenCreateScopes)
		os.Exit(2)
		return nil
	},
}

var authTokenListCmd = &cobra.Command{
	Use:   "list",
	Short: "List API tokens",
	Long: `List API tokens via the ApiTokens service.

NOTE: Requires the backend track to provide the ApiTokens service in the proto.
This command is stubbed until that lands.
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		// TODO: Call ApiTokens.ListApiTokens once the backend track adds that service.
		fmt.Fprintln(os.Stderr, "stub: ApiTokens service not yet in proto — waiting on backend track")
		os.Exit(2)
		return nil
	},
}

var authTokenRevokeCmd = &cobra.Command{
	Use:   "revoke <token-id>",
	Short: "Revoke an API token",
	Long: `Revoke an API token by ID via the ApiTokens service.

NOTE: Requires the backend track to provide the ApiTokens service in the proto.
This command is stubbed until that lands.
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		// TODO: Call ApiTokens.RevokeApiToken once the backend track adds that service.
		fmt.Fprintf(os.Stderr, "stub: ApiTokens service not yet in proto — waiting on backend track (id=%s)\n", args[0])
		os.Exit(2)
		return nil
	},
}

func init() {
	authLoginCmd.Flags().StringVar(&authLoginEndpoint, "endpoint", "", "SAE Books backend URL (e.g. http://localhost:18310)")

	authTokenCreateCmd.Flags().StringVar(&tokenCreateName, "name", "", "token name (required)")
	authTokenCreateCmd.Flags().StringVar(&tokenCreateScopes, "scope", "", "comma-separated scopes (e.g. read:invoices,write:invoices)")
	_ = authTokenCreateCmd.MarkFlagRequired("name")

	authTokenCmd.AddCommand(authTokenCreateCmd)
	authTokenCmd.AddCommand(authTokenListCmd)
	authTokenCmd.AddCommand(authTokenRevokeCmd)

	authCmd.AddCommand(authLoginCmd)
	authCmd.AddCommand(authWhoamiCmd)
	authCmd.AddCommand(authTokenCmd)
}
