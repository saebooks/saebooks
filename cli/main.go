package main

import (
	"os"

	"github.com/saebooks/saebooks/cli/cmd"
)

func main() {
	if err := cmd.Execute(); err != nil {
		os.Exit(1)
	}
}
