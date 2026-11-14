# PyInstaller recipe: one folder holding two programs that share their libraries.
#   PhotoLifeboat(.exe)       the windowed app people double-click (no console window on Windows)
#   photolifeboat-cli(.exe)   the command line, also used by the app for helper jobs such as copying the card
# A folder, not a single self-extracting file: fewer antivirus false alarms and a faster start.
# On Linux the folder is then wrapped into an AppImage by packaging/linux/build_appimage.sh.
# Build with:  pyinstaller --noconfirm --clean packaging/PhotoLifeboat.spec
import os
import sys

HERE = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821 - PyInstaller defines SPEC
WINDOWS = sys.platform == "win32"
VERSION = os.path.join(HERE, "version_info.txt") if WINDOWS else None
ICON = os.path.join(HERE, "icon.ico")
ICON = ICON if WINDOWS and os.path.exists(ICON) else None

if WINDOWS:
    hidden = ["webview.platforms.winforms", "webview.platforms.edgechromium"]
else:
    # Linux: the window uses the Qt web engine carried inside the build, and UDisks is reached over D-Bus.
    hidden = ["webview.platforms.qt", "qtpy", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineCore",
              "PyQt6.QtWebChannel", "PyQt6.QtNetwork", "jeepney", "jeepney.io.blocking"]

a = Analysis([os.path.join(HERE, "gui.py"), os.path.join(HERE, "cli.py")],  # noqa: F821
             hiddenimports=hidden,
             excludes=["tkinter.test"] + ([] if WINDOWS else ["PyQt5", "PySide2", "PySide6", "gi"]))
if not WINDOWS:
    # PyInstaller collects all of Qt (over 600 MB). The window needs only the web engine and what it is
    # linked against; the rest (3D, multimedia, QML modules, the Qt translations, the GTK theme that
    # would drag in a second copy of GTK) is left out, which brings the folder to about a third.
    import re
    KEEP_QT = {"Core", "DBus", "EglFSDeviceIntegration", "EglFsKmsSupport", "Gui", "Network", "OpenGL",
               "Positioning", "PrintSupport", "Qml", "QmlMeta", "QmlModels", "QmlWorkerScript", "Quick",
               "QuickWidgets", "Svg", "WaylandClient", "WaylandEglClientHwIntegration", "WebChannel",
               "WebEngineCore", "WebEngineWidgets", "Widgets", "WlShellIntegration", "XcbQpa"}

    def wanted(entry):
        name = entry[0].replace("\\", "/")
        m = re.search(r"libQt6(\w+)\.so", name)
        if m and m.group(1) not in KEEP_QT:
            return False
        if "/Qt6/qml/" in name or "/platformthemes/libqgtk3" in name or re.search(r"(^|/)lib(gtk|gdk)-3\.so", name):
            return False
        if "/Qt6/translations/" in name and not name.endswith("qtwebengine_locales/en-US.pak"):
            return False
        return True

    a.binaries = [e for e in a.binaries if wanted(e)]
    a.datas = [e for e in a.datas if wanted(e)]

pyz = PYZ(a.pure)  # noqa: F821
gui = [s for s in a.scripts if s[0] == "gui"]
cli = [s for s in a.scripts if s[0] == "cli"]
common = [s for s in a.scripts if s[0] not in ("gui", "cli")]
app = EXE(pyz, common + gui, [], exclude_binaries=True, name="PhotoLifeboat", console=False,  # noqa: F821
          upx=False, version=VERSION, icon=ICON)
cmd = EXE(pyz, common + cli, [], exclude_binaries=True, name="photolifeboat-cli", console=True,  # noqa: F821
          upx=False, version=VERSION, icon=ICON)
COLLECT(app, cmd, a.binaries, a.datas, upx=False, name="PhotoLifeboat")  # noqa: F821
