"""Entry point for the frozen executable (PyInstaller needs a script, not a console_scripts entry)."""

import sys

from archrev.cli import main

if __name__ == "__main__":
    sys.exit(main())
