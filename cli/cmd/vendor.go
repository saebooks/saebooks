package cmd

// vendor.go — `sae books vendor` subcommands.
//
// Vendors are contacts in the SAE Books data model — the backend uses the same
// Contact endpoints.  The CLI provides a `vendor` subtree as a UX convenience
// that mirrors the `customer` subtree.  When the backend adds a contact_type
// field or a separate Vendor service, this file should be updated to pass the
// appropriate type filter.

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

var vendorCmd = &cobra.Command{
	Use:   "vendor",
	Short: "Manage vendors (contacts)",
	Long: `Commands for working with SAE Books vendors.

Vendors are contacts in the SAE Books data model.  This subtree uses the same
Contact RPCs as the customer subtree.

NOTE: The current proto does not include a contact_type field; all contacts are
returned regardless of vendor/customer classification.  A --type filter will be
added once the backend adds that field to ContactRecord.

Examples:
  sae books vendor list
  sae books vendor list --search "Supplier"
  sae books vendor get abc123
  sae books vendor create --name "ACME Supplies" --email "orders@acme.com"
`,
}

var (
	vendorListSearch string
	vendorListPage   int32
	vendorListSize   int32
)

var vendorListCmd = &cobra.Command{
	Use:   "list",
	Short: "List vendors",
	Long: `List all vendors (contacts).

Examples:
  sae books vendor list
  sae books vendor list --search "Supplier"
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.ListContactsRequest{
			Page:   &pb.PageRequest{Page: vendorListPage, PageSize: vendorListSize},
			Search: vendorListSearch,
		}
		resp, err := c.ListContacts(context.Background(), connect.NewRequest(req))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}

		contacts := resp.Msg.Contacts

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(contacts)
		case output.FormatYAML:
			return printer.PrintYAML(contacts)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"ID", "NAME", "EMAIL", "PHONE", "UPDATED"})
			for _, ct := range contacts {
				t.AppendRow(output.Row{ct.Id, ct.Name, ct.Email, ct.Phone, ct.UpdatedAt})
			}
			if pi := resp.Msg.PageInfo; pi != nil {
				t.AppendFooter(output.Row{"", "", "", fmt.Sprintf("Page %d", pi.Page), fmt.Sprintf("Total: %d", pi.Total)})
			}
			output.PrintTable(t)
		}
		return nil
	},
}

var vendorGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Get a vendor by ID",
	Long: `Retrieve a single vendor (contact) by ID.

Examples:
  sae books vendor get abc123
`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		resp, err := c.GetContact(context.Background(), connect.NewRequest(&pb.GetContactRequest{Id: args[0]}))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}
		ct := resp.Msg.Contact

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(ct)
		case output.FormatYAML:
			return printer.PrintYAML(ct)
		default:
			t := output.NewTable()
			t.AppendHeader(output.Row{"FIELD", "VALUE"})
			t.AppendRows([]output.Row{
				{"ID", ct.Id},
				{"Name", ct.Name},
				{"Email", ct.Email},
				{"Phone", ct.Phone},
				{"Version", fmt.Sprintf("%d", ct.Version)},
				{"Updated At", ct.UpdatedAt},
			})
			output.PrintTable(t)
		}
		return nil
	},
}

var (
	vendorCreateName  string
	vendorCreateEmail string
	vendorCreatePhone string
)

var vendorCreateCmd = &cobra.Command{
	Use:   "create",
	Short: "Create a new vendor",
	Long: `Create a new vendor (contact) in SAE Books.

Examples:
  sae books vendor create --name "ACME Supplies" --email "orders@acme.com"
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.CreateContactRequest{
			Name:  vendorCreateName,
			Email: vendorCreateEmail,
			Phone: vendorCreatePhone,
		}
		resp, err := c.CreateContact(context.Background(), connect.NewRequest(req))
		if err != nil {
			handleConnectError(err, prof.Endpoint)
			os.Exit(1)
		}
		ct := resp.Msg.Contact

		switch printer.Format {
		case output.FormatJSON:
			return printer.PrintJSON(ct)
		case output.FormatYAML:
			return printer.PrintYAML(ct)
		default:
			fmt.Printf("Created vendor %s (%s)\n", ct.Id, ct.Name)
		}
		return nil
	},
}

func init() {
	vendorListCmd.Flags().StringVar(&vendorListSearch, "search", "", "search term (name or email)")
	vendorListCmd.Flags().Int32Var(&vendorListPage, "page", 1, "page number")
	vendorListCmd.Flags().Int32Var(&vendorListSize, "page-size", 25, "results per page")

	vendorCreateCmd.Flags().StringVar(&vendorCreateName, "name", "", "vendor name (required)")
	vendorCreateCmd.Flags().StringVar(&vendorCreateEmail, "email", "", "vendor email")
	vendorCreateCmd.Flags().StringVar(&vendorCreatePhone, "phone", "", "vendor phone")
	_ = vendorCreateCmd.MarkFlagRequired("name")

	vendorCmd.AddCommand(vendorListCmd)
	vendorCmd.AddCommand(vendorGetCmd)
	vendorCmd.AddCommand(vendorCreateCmd)
}
