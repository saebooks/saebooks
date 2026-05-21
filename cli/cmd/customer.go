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

var customerCmd = &cobra.Command{
	Use:   "customer",
	Short: "Manage customers (contacts)",
	Long: `Commands for working with SAE Books customers.

Customers are contacts in the SAE Books data model.

Examples:
  sae books customer list
  sae books customer list --search "Acme"
  sae books customer get abc123
  sae books customer create --name "Acme Corp" --email "ap@acme.com"
`,
}

var (
	customerListSearch string
	customerListPage   int32
	customerListSize   int32
)

var customerListCmd = &cobra.Command{
	Use:   "list",
	Short: "List customers",
	Long: `List all customers (contacts).

Examples:
  sae books customer list
  sae books customer list --search "Acme"
  sae books customer list --output json | jq '.[].email'
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.ListContactsRequest{
			Page:   &pb.PageRequest{Page: customerListPage, PageSize: customerListSize},
			Search: customerListSearch,
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

var customerGetCmd = &cobra.Command{
	Use:   "get <id>",
	Short: "Get a customer by ID",
	Long: `Retrieve a single customer (contact) by ID.

Examples:
  sae books customer get abc123
  sae books customer get abc123 --output yaml
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
	customerCreateName  string
	customerCreateEmail string
	customerCreatePhone string
)

var customerCreateCmd = &cobra.Command{
	Use:   "create",
	Short: "Create a new customer",
	Long: `Create a new customer (contact) in SAE Books.

Examples:
  sae books customer create --name "Acme Corp" --email "ap@acme.com"
  sae books customer create --name "John Smith" --email "john@example.com" --phone "+61400000000"
`,
	RunE: func(cmd *cobra.Command, args []string) error {
		c, printer, err := resolveClient()
		if err != nil {
			return err
		}

		cfg, _ := config.Load()
		prof, _ := cfg.ActiveProfile(cfg.ActiveProfileName(profileFlag))

		req := &pb.CreateContactRequest{
			Name:  customerCreateName,
			Email: customerCreateEmail,
			Phone: customerCreatePhone,
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
			fmt.Printf("Created customer %s (%s)\n", ct.Id, ct.Name)
		}
		return nil
	},
}

func init() {
	customerListCmd.Flags().StringVar(&customerListSearch, "search", "", "search term (name or email)")
	customerListCmd.Flags().Int32Var(&customerListPage, "page", 1, "page number")
	customerListCmd.Flags().Int32Var(&customerListSize, "page-size", 25, "results per page")

	customerCreateCmd.Flags().StringVar(&customerCreateName, "name", "", "customer name (required)")
	customerCreateCmd.Flags().StringVar(&customerCreateEmail, "email", "", "customer email")
	customerCreateCmd.Flags().StringVar(&customerCreatePhone, "phone", "", "customer phone")
	_ = customerCreateCmd.MarkFlagRequired("name")

	customerCmd.AddCommand(customerListCmd)
	customerCmd.AddCommand(customerGetCmd)
	customerCmd.AddCommand(customerCreateCmd)
}
