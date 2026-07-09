package cmd

import (
	"context"
	"fmt"
	"os"

	"connectrpc.com/connect"
	"github.com/spf13/cobra"

	pb "github.com/saebooks/saebooks/cli/gen/saebooks"
	"github.com/saebooks/saebooks/cli/internal/config"
	"github.com/saebooks/saebooks/cli/internal/output"
)

var jeCmd = &cobra.Command{
	Use:   "je",
	Short: "View journal entries",
	Long: `Commands for viewing SAE Books journal entries.

Examples:
  sae books je list
  sae books je list --status POSTED
  sae books je get abc123
`,
}

var (
	jeListStatus string
	jeListPage   int32
	jeListSize   int32
)

var jeListCmd = &cobra.Command{
	Use:   "list",
	Short: "List journal entries",
	Long: `List journal entries, optionally filtered by status.

Status values: DRAFT, POSTED, REVERSED

Examples:
  sae books je list
  sae books je list --status POSTED
  sae books je list --output json
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.ListJournalEntriesRequest{
			Page:   &pb.PageRequest{Page: jeListPage, PageSize: jeListSize},
			Status: jeListStatus,
		}
		resp, err := c.ListJournalEntries(context.Background(), connect.NewRequest(req))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}

		entries := resp.Msg.Entries

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(entries)
		case output.FormatYAML:
			return printer.PrintYAML(entries)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"ID", "REF", "DATE", "DESCRIPTION", "STATUS"})
			for _, e := range entries {
				desc := e.Description
				if len(desc) > 50 {
					desc = desc[:47] + "..."
				}
				t.AppendRow(output.Row{e.Id, e.Ref, e.EntryDate, desc, e.Status})
			}
			if pi := resp.Msg.PageInfo; pi != nil {
				t.AppendFooter(output.Row{"", "", "", fmt.Sprintf("Page %d", pi.Page), fmt.Sprintf("Total: %d", pi.Total)})
			}
			output.PrintTable(t)
		}
		return nil
	},
}

var jeGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Get a journal entry by ID",
	Long: `Retrieve a single journal entry by its ID.

Examples:
  sae books je get abc123
  sae books je get abc123 --output yaml
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		resp, err := c.GetJournalEntry(context.Background(), connect.NewRequest(&pb.GetJournalEntryRequest{Id: args[0]}))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}
		e := resp.Msg.Entry

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(e)
		case output.FormatYAML:
			return printer.PrintYAML(e)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"FIELD", "VALUE"})
			t.AppendRows([]output.Row{
				{"ID", e.Id},
				{"Ref", e.Ref},
				{"Date", e.EntryDate},
				{"Description", e.Description},
				{"Status", e.Status},
				{"Version", fmt.Sprintf("%d", e.Version)},
			})
			output.PrintTable(t)
		}
		return nil
	},
}

func init() {
	jeListCmd.Flags().StringVar(&jeListStatus, "status", "", "filter by status (DRAFT|POSTED|REVERSED)")
	jeListCmd.Flags().Int32Var(&jeListPage, "page", 1, "page number")
	jeListCmd.Flags().Int32Var(&jeListSize, "page-size", 25, "results per page")

	jeCmd.AddCommand(jeListCmd)
	jeCmd.AddCommand(jeGetCmd)
}
