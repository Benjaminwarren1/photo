"""Entry point for PhotoLifeboat.exe, the windowed program people double-click. It has no console window,
so it always starts the wizard; the command line is photolifeboat-cli.exe beside it. The Linux build uses
the same file for its PhotoLifeboat program."""
import os
import sys

# A windowed program has no standard output. Anything that still prints must not crash it.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

if sys.platform.startswith("linux"):
    # The window uses the Qt web engine inside the build. Its own sandbox needs kernel features that some
    # systems switch off for downloaded programs (Ubuntu 24.04 does), which stops the window opening; the
    # window only ever shows this program's own page from 127.0.0.1, so it is not needed here.
    os.environ.setdefault("QT_API", "pyqt6")
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from photolifeboat import wizard  # noqa: E402

if __name__ == "__main__":
    sys.exit(wizard.run())
