#!/usr/bin/env python3
"""One-shot migration: rewrite already-exported files to use Them/Me speaker labels.

Old:  **[system]** ...   /  **[microphone]** ...
New:  **Them:** ...      /  **Me:** ...
"""

import re
import sys
from pathlib import Path

from granola_core import DEFAULT_OUT_ROOT

SUBS = [
    (re.compile(r"\*\*\[system\]\*\*"), "**Them:**"),
    (re.compile(r"\*\*\[microphone\]\*\*"), "**Me:**"),
    (re.compile(r"\*\*\[unknown\]\*\*"), "**Unknown:**"),
]


def main():
    out_dir = (Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT_ROOT) / "transcripts"
    if not out_dir.exists():
        print(f"No such dir: {out_dir}", file=sys.stderr)
        sys.exit(1)

    changed = 0
    for p in out_dir.glob("*.md"):
        text = p.read_text()
        new_text = text
        for pat, repl in SUBS:
            new_text = pat.sub(repl, new_text)
        if new_text != text:
            p.write_text(new_text)
            changed += 1
    print(f"Updated {changed} files in {out_dir}")


if __name__ == "__main__":
    main()
