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

var paymentCmd = &cobra.Command{
	Use:   "payment",
	Short: "View payments",
	Long: `Commands for viewing SAE Books payments.

Examples:
  sae books payment list
  sae books payment list --direction INCOMING
  sae books payment get abc123
`,
}

var (
	paymentListDirection string
	paymentListPage      int32
	paymentListSize      int32
)

var paymentListCmd = &cobra.Command{
	Use:   "list",
	Short: "List payments",
	Long: `List payments, optionally filtered by direction.

Direction values: INCOMING, OUTGOING

Examples:
  sae books payment list
  sae books payment list --direction INCOMING
  sae books payment list --output json
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.ListPaymentsRequest{
			Page:      &pb.PageRequest{Page: paymentListPage, PageSize: paymentListSize},
			Direction: paymentListDirection,
		}
		resp, err := c.ListPayments(context.Background(), connect.NewRequest(req))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}

		payments := resp.Msg.Payments

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(payments)
		case output.FormatYAML:
			return printer.PrintYAML(payments)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"ID", "DATE", "DIRECTION", "AMOUNT", "METHOD", "REFERENCE", "CONTACT"})
			for _, p := range payments {
				t.AppendRow(output.Row{
					p.Id,
					p.PaymentDate,
					p.Direction,
					fmt.Sprintf("%.2f", p.Amount),
					p.Method,
					p.Reference,
					p.ContactId,
				})
			}
			if pi := resp.Msg.PageInfo; pi != nil {
				t.AppendFooter(output.Row{"", "", "", "", "", fmt.Sprintf("Page %d", pi.Page), fmt.Sprintf("Total: %d", pi.Total)})
			}
			output.PrintTable(t)
		}
		return nil
	},
}

var paymentGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Get a payment by ID",
	Long: `Retrieve a single payment by its ID.

Examples:
  sae books payment get abc123
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		resp, err := c.GetPayment(context.Background(), connect.NewRequest(&pb.GetPaymentRequest{Id: args[0]}))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}
		p := resp.Msg.Payment

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(p)
		case output.FormatYAML:
			return printer.PrintYAML(p)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"FIELD", "VALUE"})
			t.AppendRows([]output.Row{
				{"ID", p.Id},
				{"Contact ID", p.ContactId},
				{"Date", p.PaymentDate},
				{"Direction", p.Direction},
				{"Amount", fmt.Sprintf("%.2f", p.Amount)},
				{"Method", p.Method},
				{"Reference", p.Reference},
				{"Version", fmt.Sprintf("%d", p.Version)},
			})
			output.PrintTable(t)
		}
		return nil
	},
}

func init() {
	paymentListCmd.Flags().StringVar(&paymentListDirection, "direction", "", "filter by direction (INCOMING|OUTGOING)")
	paymentListCmd.Flags().Int32Var(&paymentListPage, "page", 1, "page number")
	paymentListCmd.Flags().Int32Var(&paymentListSize, "page-size", 25, "results per page")

	paymentCmd.AddCommand(paymentListCmd)
	paymentCmd.AddCommand(paymentGetCmd)
}
