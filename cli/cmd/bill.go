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

var billCmd = &cobra.Command{
	Use:   "bill",
	Short: "Manage bills (accounts payable)",
	Long: `Commands for working with SAE Books bills (AP).

Examples:
  sae books bill list
  sae books bill list --status UNPAID
  sae books bill get abc123
`,
}

var (
	billListStatus string
	billListPage   int32
	billListSize   int32
)

var billListCmd = &cobra.Command{
	Use:   "list",
	Short: "List bills",
	Long: `List bills, optionally filtered by status.

Examples:
  sae books bill list
  sae books bill list --status UNPAID
  sae books bill list --output json
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.ListBillsRequest{
			Page:   &pb.PageRequest{Page: billListPage, PageSize: billListSize},
			Status: billListStatus,
		}
		resp, err := c.ListBills(context.Background(), connect.NewRequest(req))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}

		bills := resp.Msg.Bills

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(bills)
		case output.FormatYAML:
			return printer.PrintYAML(bills)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"ID", "NUMBER", "VENDOR", "ISSUE DATE", "DUE DATE", "STATUS", "TOTAL", "PAID"})
			for _, b := range bills {
				t.AppendRow(output.Row{
					b.Id,
					b.Number,
					b.ContactId,
					b.IssueDate,
					b.DueDate,
					b.Status,
					fmt.Sprintf("%.2f", b.Total),
					fmt.Sprintf("%.2f", b.AmountPaid),
				})
			}
			if pi := resp.Msg.PageInfo; pi != nil {
				t.AppendFooter(output.Row{"", "", "", "", "", "", fmt.Sprintf("Page %d", pi.Page), fmt.Sprintf("Total: %d", pi.Total)})
			}
			output.PrintTable(t)
		}
		return nil
	},
}

var billGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Get a bill by ID",
	Long: `Retrieve a single bill by its ID.

Examples:
  sae books bill get abc123
  sae books bill get abc123 --output json
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		resp, err := c.GetBill(context.Background(), connect.NewRequest(&pb.GetBillRequest{Id: args[0]}))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}
		b := resp.Msg.Bill

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(b)
		case output.FormatYAML:
			return printer.PrintYAML(b)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"FIELD", "VALUE"})
			t.AppendRows([]output.Row{
				{"ID", b.Id},
				{"Number", b.Number},
				{"Vendor ID", b.ContactId},
				{"Issue Date", b.IssueDate},
				{"Due Date", b.DueDate},
				{"Status", b.Status},
				{"Total", fmt.Sprintf("%.2f", b.Total)},
				{"Amount Paid", fmt.Sprintf("%.2f", b.AmountPaid)},
				{"Version", fmt.Sprintf("%d", b.Version)},
			})
			output.PrintTable(t)
		}
		return nil
	},
}

func init() {
	billListCmd.Flags().StringVar(&billListStatus, "status", "", "filter by status")
	billListCmd.Flags().Int32Var(&billListPage, "page", 1, "page number")
	billListCmd.Flags().Int32Var(&billListSize, "page-size", 25, "results per page")

	billCmd.AddCommand(billListCmd)
	billCmd.AddCommand(billGetCmd)
}
