package config

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/BurntSushi/toml"
)

const defaultConfigDir = ".config/saebooks"
const configFileName = "config.toml"

// Profile holds per-profile settings.
type Profile struct {
	Endpoint string `toml:"endpoint"`
	APIToken string `toml:"api_token"`
	JWT      string `toml:"jwt"`
	Output   string `toml:"output"`
}

// Config is the top-level config structure.
type Config struct {
	DefaultProfile string             `toml:"default_profile"`
	Profiles       map[string]Profile `toml:"profiles"`

	// path is the resolved on-disk path; not serialised.
	path string
}

// ConfigPath returns the path to the config file.
func ConfigPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("could not determine home directory: %w", err)
	}
	return filepath.Join(home, defaultConfigDir, configFileName), nil
}

// Load reads the config file. Returns an empty default config if the file does
// not exist yet.
func Load() (*Config, error) {
	path, err := ConfigPath()
	if err != nil {
		return nil, err
	}

	cfg := &Config{
		DefaultProfile: "local",
		Profiles:       make(map[string]Profile),
		path:           path,
	}

	if _, err := os.Stat(path); os.IsNotExist(err) {
		// No config file yet — return empty defaults.
		cfg.Profiles["local"] = Profile{
			Endpoint: "http://localhost:18310",
			Output:   "table",
		}
		return cfg, nil
	}

	if _, err := toml.DecodeFile(path, cfg); err != nil {
		return nil, fmt.Errorf("error reading config file %s: %w", path, err)
	}
	cfg.path = path
	if cfg.Profiles == nil {
		cfg.Profiles = make(map[string]Profile)
	}
	return cfg, nil
}

// Save writes the config back to disk.
func (c *Config) Save() error {
	if err := os.MkdirAll(filepath.Dir(c.path), 0700); err != nil {
		return fmt.Errorf("could not create config directory: %w", err)
	}
	f, err := os.OpenFile(c.path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return fmt.Errorf("could not open config file for writing: %w", err)
	}
	defer f.Close()
	enc := toml.NewEncoder(f)
	return enc.Encode(c)
}

// ActiveProfileName returns the profile name to use, respecting the env var
// override and the --profile flag (passed explicitly).
func (c *Config) ActiveProfileName(flagOverride string) string {
	if flagOverride != "" {
		return flagOverride
	}
	if env := os.Getenv("SAEBOOKS_PROFILE"); env != "" {
		return env
	}
	if c.DefaultProfile != "" {
		return c.DefaultProfile
	}
	return "local"
}

// ActiveProfile returns the Profile for the given name (or default).
func (c *Config) ActiveProfile(name string) (Profile, error) {
	p, ok := c.Profiles[name]
	if !ok {
		return Profile{}, fmt.Errorf("profile %q not found in config — run `sae books auth login` to create it", name)
	}
	return p, nil
}

// SetProfile stores a profile under name and saves config.
func (c *Config) SetProfile(name string, p Profile) error {
	c.Profiles[name] = p
	return c.Save()
}

// Path returns the resolved config file path.
func (c *Config) Path() string {
	return c.path
}
