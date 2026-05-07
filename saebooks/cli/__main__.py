"""Allow python -m saebooks.cli sync-feeds to work.

Without this file, python -m saebooks.cli errors with
"saebooks.cli is a package and cannot be directly executed" because
the directory takes precedence over the legacy saebooks/cli.py.
This shim resolves the precedence by giving the package its own
entry point, calling the same main defined in __init__.py.
"""
from saebooks.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
