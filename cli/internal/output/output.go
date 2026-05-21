package output

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/jedib0t/go-pretty/v6/table"
	"github.com/jedib0t/go-pretty/v6/text"
	"gopkg.in/yaml.v3"
)

// Format represents the output format.
type Format string

const (
	FormatTable Format = "table"
	FormatJSON  Format = "json"
	FormatYAML  Format = "yaml"
)

// IsTTY returns true when stdout is an interactive terminal.
func IsTTY() bool {
	fi, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return (fi.Mode() & os.ModeCharDevice) != 0
}

// DefaultFormat returns the appropriate default: table for TTY, json otherwise.
func DefaultFormat(profileDefault string) Format {
	if profileDefault != "" {
		return Format(profileDefault)
	}
	if !IsTTY() {
		return FormatJSON
	}
	return FormatTable
}

// Printer handles rendering output in any supported format.
type Printer struct {
	Format  Format
	Compact bool
}

// New creates a Printer.
func New(f Format, compact bool) *Printer {
	return &Printer{Format: f, Compact: compact}
}

// PrintJSON serialises v as JSON to stdout.
func (p *Printer) PrintJSON(v any) error {
	var b []byte
	var err error
	if p.Compact {
		b, err = json.Marshal(v)
	} else {
		b, err = json.MarshalIndent(v, "", "  ")
	}
	if err != nil {
		return fmt.Errorf("json marshal: %w", err)
	}
	fmt.Println(string(b))
	return nil
}

// PrintYAML serialises v as YAML to stdout.
func (p *Printer) PrintYAML(v any) error {
	b, err := yaml.Marshal(v)
	if err != nil {
		return fmt.Errorf("yaml marshal: %w", err)
	}
	fmt.Print(string(b))
	return nil
}

// NewTable returns a configured go-pretty table writer.
func NewTable() table.Writer {
	t := table.NewWriter()
	t.SetOutputMirror(os.Stdout)
	t.SetStyle(table.StyleLight)
	t.Style().Options.DrawBorder = false
	t.Style().Options.SeparateColumns = true
	t.Style().Options.SeparateHeader = true
	t.Style().Color.Header = text.Colors{text.Bold}
	return t
}

// PrintTable renders a prebuilt table writer.
func PrintTable(t table.Writer) {
	t.Render()
}

// Row is a convenience alias for table.Row.
type Row = table.Row

// Errorf prints a formatted error message to stderr.
func Errorf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "error: "+format+"\n", args...)
}
