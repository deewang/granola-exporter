#!/usr/bin/env python3
"""CLI: extract all Granola meeting notes + transcripts to Markdown."""

import argparse
import sys
import time
import urllib.error
from pathlib import Path

from granola_core import (
    AuthError,
    DEFAULT_OUT_ROOT,
    collect_existing_meta,
    fetch_transcript,
    load_access_token,
    load_documents,
    meeting_filename,
    scan_existing,
    write_index,
    write_meeting_file,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-transcripts", action="store_true")
    args = parser.parse_args()

    out_root = Path(args.out)
    out_dir = out_root / "transcripts"

    try:
        token, source, remaining = load_access_token()
    except AuthError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Using {source} (~{remaining}s remaining)", file=sys.stderr)

    docs = load_documents()
    if args.limit:
        docs = docs[: args.limit]

    existing = scan_existing(out_dir)
    entries = collect_existing_meta(out_dir)
    entries_by_name = {e.filename: e for e in entries}

    fetched = no_tx = errors = skipped = 0
    total = len(docs)

    for i, doc in enumerate(docs, 1):
        fname = meeting_filename(doc)
        if fname in existing and not args.force:
            skipped += 1
            continue

        segments = None
        if not args.no_transcripts:
            try:
                resp = fetch_transcript(doc["id"], token)
                if resp is None:
                    no_tx += 1
                elif isinstance(resp, list):
                    segments = resp
                    if segments:
                        fetched += 1
                elif isinstance(resp, dict) and "transcript" in resp:
                    segments = resp["transcript"]
                    if segments:
                        fetched += 1
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    print(f"\nAuth failed ({e.code}). Open Granola app to refresh token.", file=sys.stderr)
                    sys.exit(2)
                errors += 1
                print(f"  [error {e.code}] {fname}", file=sys.stderr)
            except Exception as e:
                errors += 1
                print(f"  [error] {fname}: {e}", file=sys.stderr)
            time.sleep(args.sleep)

        _, meta = write_meeting_file(out_dir, doc, segments)
        entries_by_name[meta.filename] = meta

        if i % 10 == 0 or i == total:
            print(f"[{i}/{total}] fetched={fetched} no_tx={no_tx} skipped={skipped} errors={errors}")

    write_index(out_root, list(entries_by_name.values()))
    print(f"\nDone. Index: {out_root / 'INDEX.md'}")


if __name__ == "__main__":
    main()
