#!/bin/bash
# Double-click to launch the Granola Export GUI.
cd "$(dirname "$0")"
exec /usr/bin/env python3 gui.py
