"""Entry point for the packaged program. Double-clicking it starts the wizard."""
import sys

from photorecover.cli import main

if __name__ == "__main__":
    sys.exit(main())
