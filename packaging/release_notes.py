"""Prints the CHANGELOG.md section for one version, followed by the download instructions every release
carries. Used by the release workflow; fails if the changelog has no section for that version."""
import re
import sys
from pathlib import Path

FOOTER = """
## Download

**Windows 10 and 11:** download `PhotoLifeboat-{v}-setup.exe` below and open it. It installs in a few seconds,
without needing an administrator, and adds Photo Lifeboat to the Start menu. Uninstall it from Settings > Apps
like any other program.

Windows may say "Windows protected your PC" because the program is not code-signed yet. Click **More info**,
then **Run anyway**.

Prefer not to install anything? `PhotoLifeboat-{v}-windows-portable.zip` is the same program in a folder:
extract it and double-click **PhotoLifeboat**. `SHA256SUMS.txt` lets anyone check a download is the one built here.

Before you start: stop using the card that lost the photos. See the [README](https://github.com/Benjaminwarren1/photo#readme).
"""


def section(changelog: str, version: str) -> str:
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", changelog, re.S | re.M)
    return m.group(1).strip() if m else ""


def main() -> int:
    v = sys.argv[1]
    body = section((Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8"), v)
    if not body:
        print(f"CHANGELOG.md has no section for {v}. Add '## {v}' with what changed.", file=sys.stderr)
        return 1
    print(body + "\n" + FOOTER.format(v=v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
