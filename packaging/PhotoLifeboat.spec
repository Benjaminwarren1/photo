# PyInstaller recipe for the Windows build: one folder holding two programs that share their libraries.
#   PhotoLifeboat.exe       the windowed app people double-click (no console window)
#   photolifeboat-cli.exe   the command line, also used by the app for helper jobs such as copying the card
# A folder, not a single self-extracting file: fewer antivirus false alarms and a faster start.
# Build with:  pyinstaller --noconfirm --clean packaging/PhotoLifeboat.spec
import os

HERE = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821 - PyInstaller defines SPEC
VERSION = os.path.join(HERE, "version_info.txt")
ICON = os.path.join(HERE, "icon.ico")

a = Analysis([os.path.join(HERE, "gui.py"), os.path.join(HERE, "cli.py")],  # noqa: F821
             hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium"],
             excludes=["tkinter.test"])
pyz = PYZ(a.pure)  # noqa: F821
gui = [s for s in a.scripts if s[0] == "gui"]
cli = [s for s in a.scripts if s[0] == "cli"]
common = [s for s in a.scripts if s[0] not in ("gui", "cli")]
app = EXE(pyz, common + gui, [], exclude_binaries=True, name="PhotoLifeboat", console=False,  # noqa: F821
          upx=False, version=VERSION, icon=ICON if os.path.exists(ICON) else None)
cmd = EXE(pyz, common + cli, [], exclude_binaries=True, name="photolifeboat-cli", console=True,  # noqa: F821
          upx=False, version=VERSION, icon=ICON if os.path.exists(ICON) else None)
COLLECT(app, cmd, a.binaries, a.datas, upx=False, name="PhotoLifeboat")  # noqa: F821
