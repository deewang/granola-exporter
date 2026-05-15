#!/bin/bash
# Compiles Recorder.swift → ./recorder/recorder (universal2 Mach-O binary)
set -euo pipefail
cd "$(dirname "$0")"

# Build a universal binary so it works on both Apple Silicon and Intel.
# `-target` sets the deployment target; ScreenCaptureKit requires macOS 13+.
swiftc -O \
    -target arm64-apple-macos13 \
    Recorder.swift \
    -o recorder

echo "✓ Built recorder (arm64 native, $(du -h recorder | cut -f1))"
