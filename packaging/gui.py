"""Entry point for PhotoLifeboat.exe, the windowed program people double-click. It has no console window,
so it always starts the wizard; the command line is photolifeboat-cli.exe beside it."""
import os
import sys

# A windowed program has no standard output. Anything that still prints must not crash it.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from photolifeboat import wizard  # noqa: E402

if __name__ == "__main__":
    sys.exit(wizard.run())
