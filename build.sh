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
    --collect-all customtkinter \
    $ICON_FLAG \
    gui.py

echo ""
echo "✅ Built: dist/Granola Export.app"
echo ""
echo "Next steps:"
echo "  • Test:    open 'dist/Granola Export.app'"
echo "  • Install: cp -R 'dist/Granola Export.app' /Applications/"
echo "  • Ship:    ./make-dmg.sh   (creates a Granola-Export.dmg)"
