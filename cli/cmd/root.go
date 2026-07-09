package cmd

import (
	"fmt"
	"os"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"

	"github.com/saebooks/saebooks/cli/gen/saebooksconnect"
	"github.com/saebooks/saebooks/cli/internal/client"
	"github.com/saebooks/saebooks/cli/internal/config"
	"github.com/saebooks/saebooks/cli/internal/output"
)

// These are set at build time via -ldflags.
var (
	Version = "dev"
	Commit  = "none"
	Date    = "unknown"
)

// Global flags.
var (
	profileFlag string
	outputFlag  string
	compactFlag bool
	tokenFlag   string
)

// rootCmd is the entry for `sae books`.
var rootCmd = &cobra.Command{
	Use:   "sae books",
	Short: "SAE Books CLI — manage your accounting data from the terminal",
	Long: `SAE Books CLI

A command-line interface to the SAE Books accounting backend.
Talks to the backend over Connect-RPC (grpc-web compatible).

Configuration: ~/.config/saebooks/config.toml
Profile override: --profile <name> or SAEBOOKS_PROFILE env var

Examples:
  sae books auth login --endpoint http://localhost:18310
  sae books invoice list
  sae books customer list --search "Acme"
  sae books je list --status POSTED
`,
	Version: fmt.Sprintf("%s (commit %s, built %s)", Version, Commit, Date),
}

// Execute is called from main.
func Execute() error {
	return rootCmd.Execute()
}

func init() {
	rootCmd.PersistentFlags().StringVar(&profileFlag, "profile", "", "config profile to use (overrides SAEBOOKS_PROFILE and default_profile)")
	rootCmd.PersistentFlags().StringVarP(&outputFlag, "output", "o", "", "output format: table|json|yaml (default: table for TTY, json otherwise)")
	rootCmd.PersistentFlags().BoolVar(&compactFlag, "compact", false, "compact JSON output (no pretty-print)")
	rootCmd.PersistentFlags().StringVar(&tokenFlag, "token", "", "API token or JWT (overrides profile, SAEBOOKS_TOKEN env)")

	rootCmd.AddCommand(authCmd)
	rootCmd.AddCommand(invoiceCmd)
	rootCmd.AddCommand(customerCmd)
	rootCmd.AddCommand(vendorCmd)
	rootCmd.AddCommand(billCmd)
	rootCmd.AddCommand(paymentCmd)
	rootCmd.AddCommand(jeCmd)
}

// resolveClient loads config and returns an authenticated client + printer.
func resolveClient() (saebooksconnect.SAEBooksClient, *output.Printer, error) {
	cfg, err := config.Load()
	if err != nil {
		return nil, nil, fmt.Errorf("could not load config: %w", err)
	}

	profileName := cfg.ActiveProfileName(profileFlag)
	prof, err := cfg.ActiveProfile(profileName)
	if err != nil {
		return nil, nil, err
	}

	c, err := client.New(prof, client.Options{ExplicitToken: tokenFlag})
	if err != nil {
		return nil, nil, err
	}

	// Determine output format.
	fmt := output.Format(outputFlag)
	if fmt == "" {
		fmt = output.DefaultFormat(prof.Output)
	}
	printer := output.New(fmt, compactFlag)
	return c, printer, nil
}

// handleConnectError prints a useful error for Connect-RPC errors and returns
// the appropriate exit code.
func handleConnectError(err error, endpoint string) {
	if err == nil {
		return
	}
	cerr, ok := err.(*connect.Error)
	if !ok {
		fmt.Fprintf(os.Stderr, "error: couldn't reach %s: %v\n", endpoint, err)
		return
	}
	switch cerr.Code() {
	case connect.CodeUnauthenticated:
		fmt.Fprintf(os.Stderr, "error: unauthenticated (%s) — run `sae books auth login` first\n", cerr.Message())
	case connect.CodeNotFound:
		fmt.Fprintf(os.Stderr, "error: not found — %s\n", cerr.Message())
	case connect.CodePermissionDenied:
		fmt.Fprintf(os.Stderr, "error: permission denied — %s\n", cerr.Message())
	default:
		fmt.Fprintf(os.Stderr, "error [%s]: %s\n", cerr.Code(), cerr.Message())
	}
}

