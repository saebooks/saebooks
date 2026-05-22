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

var invoiceCmd = &cobra.Command{
	Use:   "invoice",
	Short: "Manage invoices",
	Long: `Commands for working with SAE Books invoices.

Examples:
  sae books invoice list
  sae books invoice list --status DRAFT
  sae books invoice get INV-001
`,
}

var (
	invoiceListStatus string
	invoiceListPage   int32
	invoiceListSize   int32
)

var invoiceListCmd = &cobra.Command{
	Use:   "list",
	Short: "List invoices",
	Long: `List invoices, optionally filtered by status.

Status values: DRAFT, SENT, PAID, OVERDUE, VOIDED

Examples:
  sae books invoice list
  sae books invoice list --status DRAFT
  sae books invoice list --status PAID --output json
  sae books invoice list --page 2 --page-size 50
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.ListInvoicesRequest{
			Page:   &pb.PageRequest{Page: invoiceListPage, PageSize: invoiceListSize},
			Status: invoiceListStatus,
		}
		resp, err := c.ListInvoices(context.Background(), connect.NewRequest(req))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}

		invoices := resp.Msg.Invoices

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(invoices)
		case output.FormatYAML:
			return printer.PrintYAML(invoices)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"ID", "NUMBER", "CONTACT", "ISSUE DATE", "DUE DATE", "STATUS", "TOTAL", "PAID"})
			for _, inv := range invoices {
				t.AppendRow(output.Row{
					inv.Id,
					inv.Number,
					inv.ContactId,
					inv.IssueDate,
					inv.DueDate,
					inv.Status,
					fmt.Sprintf("%.2f", inv.Total),
					fmt.Sprintf("%.2f", inv.AmountPaid),
				})
			}
			if pi := resp.Msg.PageInfo; pi != nil {
				t.AppendFooter(output.Row{"", "", "", "", "", "", fmt.Sprintf("Page %d/%d", pi.Page, (pi.Total+pi.PageSize-1)/pi.PageSize), fmt.Sprintf("Total: %d", pi.Total)})
			}
			output.PrintTable(t)
		}
		return nil
	},
}

var invoiceGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Get a single invoice by ID",
	Long: `Retrieve a single invoice by its ID.

Examples:
  sae books invoice get abc123
  sae books invoice get abc123 --output json
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		resp, err := c.GetInvoice(context.Background(), connect.NewRequest(&pb.GetInvoiceRequest{Id: args[0]}))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}
		inv := resp.Msg.Invoice

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(inv)
		case output.FormatYAML:
			return printer.PrintYAML(inv)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"FIELD", "VALUE"})
			t.AppendRows([]output.Row{
				{"ID", inv.Id},
				{"Number", inv.Number},
				{"Contact ID", inv.ContactId},
				{"Issue Date", inv.IssueDate},
				{"Due Date", inv.DueDate},
				{"Status", inv.Status},
				{"Total", fmt.Sprintf("%.2f", inv.Total)},
				{"Amount Paid", fmt.Sprintf("%.2f", inv.AmountPaid)},
				{"Version", fmt.Sprintf("%d", inv.Version)},
			})
			output.PrintTable(t)
		}
		return nil
	},
}

// TODO: invoice create — waiting on backend to clarify line-item proto shape.
// The current proto only has InvoiceRecord (read model); no CreateInvoiceRequest yet.
var invoiceCreateCmd = &cobra.Command{
	Use:   "create",
	Short: "Create an invoice (stub — not yet implemented)",
	Long: `Create a new invoice.

NOTE: Not yet implemented. The proto does not yet include a CreateInvoiceRequest
message. Waiting on the backend track to add it.
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		fmt.Fprintln(os.Stderr, "stub: CreateInvoice not yet in proto — waiting on backend track")
		os.Exit(2)
		return nil
	},
}

// TODO: invoice send — waiting on backend to add SendInvoice RPC.
var invoiceSendCmd = &cobra.Command{
	Use:   "send <id>",
	Short: "Send an invoice (stub — not yet implemented)",
	Long: `Send an invoice to the contact's email address.

NOTE: Not yet implemented. Waiting on the backend track to add a SendInvoice RPC.
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		fmt.Fprintf(os.Stderr, "stub: SendInvoice not yet in proto — waiting on backend track (id=%s)\n", args[0])
		os.Exit(2)
		return nil
	},
}

func init() {
	invoiceListCmd.Flags().StringVar(&invoiceListStatus, "status", "", "filter by status (DRAFT|SENT|PAID|OVERDUE|VOIDED)")
	invoiceListCmd.Flags().Int32Var(&invoiceListPage, "page", 1, "page number")
	invoiceListCmd.Flags().Int32Var(&invoiceListSize, "page-size", 25, "results per page")

	invoiceCmd.AddCommand(invoiceListCmd)
	invoiceCmd.AddCommand(invoiceGetCmd)
	invoiceCmd.AddCommand(invoiceCreateCmd)
	invoiceCmd.AddCommand(invoiceSendCmd)
}
