#!/usr/bin/env bash
# Wraps the PyInstaller folder (dist/PhotoLifeboat) into one AppImage file people can download and run.
# Usage: packaging/linux/build_appimage.sh VERSION
# Needs: dist/PhotoLifeboat from "pyinstaller packaging/PhotoLifeboat.spec", and network access the first
# time (to fetch appimagetool). The result is PhotoLifeboat-VERSION-x86_64.AppImage in the current folder.
set -euo pipefail
VERSION="${1:?give the version, e.g. 0.2.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
APPDIR="$(mktemp -d)/PhotoLifeboat.AppDir"
TOOL="${APPIMAGETOOL:-$ROOT/build/appimagetool-x86_64.AppImage}"

mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$ROOT/dist/PhotoLifeboat" "$APPDIR/usr/lib/photolifeboat"
cp "$HERE/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp "$HERE/photolifeboat.desktop" "$APPDIR/photolifeboat.desktop"
cp "$HERE/photolifeboat.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/docs/icon.png" "$APPDIR/photolifeboat.png"
cp "$ROOT/docs/icon.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/photolifeboat.png"
ln -s photolifeboat.png "$APPDIR/.DirIcon"

# appimagetool from the AppImage project, a fixed release checked against its known fingerprint. Its
# AppImages carry the newer runtime that does not need the old libfuse2, which Ubuntu 22.04 and later no
# longer install by default.
TOOL_URL=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
TOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
if [ ! -f "$TOOL" ]; then
    mkdir -p "$(dirname "$TOOL")"
    curl -fsSL -o "$TOOL.part" "$TOOL_URL"
    mv "$TOOL.part" "$TOOL"
fi
echo "$TOOL_SHA256  $TOOL" | sha256sum -c --quiet || { echo "appimagetool does not match its fingerprint" >&2; exit 1; }
chmod +x "$TOOL"
OUT="PhotoLifeboat-$VERSION-x86_64.AppImage"
# Runs without FUSE too (build machines often have none).
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "$OUT"
