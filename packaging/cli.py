"""Entry point for photolifeboat-cli.exe, the command-line program (also used by the windowed one for helper jobs
such as copying the card, which need standard output)."""
import sys

from photolifeboat.cli import main

if __name__ == "__main__":
    sys.exit(main())
