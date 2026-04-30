#!/bin/bash
# Build a self-contained "Granola Export.app" using PyInstaller.
# Output: dist/Granola Export.app
#
# Run from the repo root:
#   ./build.sh
#
# Optional flags via env vars:
#   ICON=path/to/icon.icns ./build.sh   # custom app icon
#   CLEAN=1 ./build.sh                  # remove old build artefacts first

set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f VERSION ]]; then
    echo "ERROR: VERSION file missing." >&2
    exit 1
fi
VERSION="$(tr -d '[:space:]' < VERSION)"
echo "Building version $VERSION"

if [[ "${CLEAN:-0}" == "1" ]]; then
    echo "Cleaning previous build…"
    rm -rf build dist
fi

ICON_FLAG=""
if [[ -n "${ICON:-}" ]]; then
    ICON_FLAG="--icon $ICON"
elif [[ -f "icon.icns" ]]; then
    ICON_FLAG="--icon icon.icns"
fi

# Verify PyInstaller is available
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "PyInstaller not found. Installing…"
    python3 -m pip install --user pyinstaller
fi

echo "Building Granola Export.app…"
python3 -m PyInstaller \
    --name "Granola Export" \
    --windowed \
    --noconfirm \
    --clean \
    --osx-bundle-identifier com.davidwang.granolaexport \
    --hidden-import granola_core \
    --hidden-import menubar \
    --hidden-import AppKit \
    --hidden-import Foundation \
    --hidden-import objc \
    --collect-all customtkinter \
    --collect-submodules AppKit \
    --collect-submodules Foundation \
    --add-data "VERSION:." \
    $ICON_FLAG \
    gui.py

# --- Patch the generated Info.plist with proper version + metadata ---
PLIST="dist/Granola Export.app/Contents/Info.plist"
COPYRIGHT="© $(date +%Y) David Wang"

echo "Patching Info.plist with version $VERSION…"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace CFBundleDisplayName -string "Granola Export" "$PLIST"
plutil -replace NSHumanReadableCopyright -string "$COPYRIGHT" "$PLIST"

# Make the VERSION file readable inside the bundle from Python via __file__/.. lookups
mkdir -p "dist/Granola Export.app/Contents/Resources"
cp VERSION "dist/Granola Export.app/Contents/Resources/VERSION"

echo ""
echo "✅ Built: dist/Granola Export.app  (v$VERSION)"
echo ""
echo "Next steps:"
echo "  • Test:    open 'dist/Granola Export.app'"
echo "  • Install: cp -R 'dist/Granola Export.app' /Applications/"
echo "  • Ship:    ./make-dmg.sh   (uses VERSION file)"
