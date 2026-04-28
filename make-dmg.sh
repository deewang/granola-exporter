#!/bin/bash
# Package dist/Granola Export.app into a drag-to-install DMG.
# Output: Granola-Export-<version>.dmg
#
# Run AFTER ./build.sh has produced dist/Granola Export.app

set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Granola Export.app"
VERSION="${VERSION:-1.0}"
DMG_NAME="Granola-Export-${VERSION}.dmg"
STAGING="dist/dmg-staging"

if [[ ! -d "$APP" ]]; then
    echo "ERROR: $APP not found. Run ./build.sh first."
    exit 1
fi

# Stage: app + symlink to /Applications for drag-to-install UX
rm -rf "$STAGING" "$DMG_NAME"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

hdiutil create \
    -volname "Granola Export" \
    -srcfolder "$STAGING" \
    -ov \
    -format UDZO \
    "$DMG_NAME"

rm -rf "$STAGING"

echo ""
echo "✅ Built: $DMG_NAME ($(du -h "$DMG_NAME" | cut -f1))"
echo ""
echo "Distribute this single file. Users:"
echo "  1. Double-click the DMG"
echo "  2. Drag 'Granola Export' onto the Applications shortcut"
echo "  3. Eject and launch from Applications"
