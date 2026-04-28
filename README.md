# Granola Exporter

A tiny macOS desktop app that exports your [Granola](https://www.granola.ai/) meeting notes and transcripts to local Markdown files — sortable, indexable, and easy to feed to an AI assistant for content creation.

No Granola Enterprise plan required. Reads the Granola desktop app's local cache + an undocumented endpoint to fetch transcripts.

![status: works on my machine]

## Features

- **One-click export** — Tkinter GUI, ships with system Python (no `pip install`).
- **Smart diffing** — auto-detects meetings you haven't exported yet and pre-ticks them.
- **Selectable** — pick which meetings to export, or hit Select All.
- **Sortable filenames** — `YYYY-MM-DD_HHMM_<slug>_<id>.md` sorts chronologically anywhere.
- **YAML frontmatter** — `id`, `title`, `date`, `participants`, `has_transcript`, `segment_count`. Easy to query.
- **Friendly speaker labels** — system audio → `**Them:**`, microphone → `**Me:**`.
- **Index file** — `INDEX.md` with a sorted Markdown table of every meeting.
- **Resumable** — re-running skips files already on disk.
- **CLI fallback** — `extract.py` for headless/scripted use.

## Requirements

- macOS (uses the Granola desktop app's local cache at `~/Library/Application Support/Granola/`)
- Python 3.9+ (system Python on modern macOS works fine — `tkinter` is bundled)
- The Granola desktop app installed and signed in

## Install

```bash
git clone https://github.com/deewang/granola-exporter.git ~/Applications/GranolaExport
```

Then drag `Granola Export.app` to your Dock or Applications folder.

## Usage

### GUI

Double-click `Granola Export.app` (or `Granola Export.command`). The app:

1. Loads all meetings from your local Granola cache.
2. Marks anything not yet exported as 🆕 NEW (light-blue rows, pre-ticked).
3. You tick / untick rows; **Select New / Select All / Clear** for bulk.
4. Click **Export Selected** → fetches transcripts via the API, writes `transcripts/*.md` and updates `INDEX.md`.

Output lands in `transcripts/` next to the app by default; pick any folder via the **…** button.

### CLI

```bash
python3 extract.py                  # all meetings, default output folder
python3 extract.py --limit 5        # test with 5
python3 extract.py --out ~/Notes    # custom output folder
python3 extract.py --force          # re-export existing files
python3 extract.py --no-transcripts # notes only, no API calls
```

## How it works

1. Reads the Granola desktop app's auth token from `~/Library/Application Support/Granola/supabase.json` (prefers `workos_tokens`, falls back to legacy `cognito_tokens`).
2. Reads the meeting list from `~/Library/Application Support/Granola/cache-v6.json`.
3. For each meeting, POSTs to `https://api.granola.ai/v1/get-document-transcript` with `Authorization: Bearer <token>`.
4. Coalesces consecutive same-source segments into paragraphs and writes Markdown.

If the token expires (~6h), open the Granola app once to refresh it, then click **Refresh** in the GUI.

## File layout

```
GranolaExport/
├── Granola Export.app          # macOS app bundle (double-clickable)
├── Granola Export.command      # alt launcher (opens in Terminal)
├── gui.py                      # Tkinter GUI
├── extract.py                  # CLI
├── granola_core.py             # shared logic (auth, API, file output)
├── migrate_speaker_labels.py   # one-shot script to update old exports
├── transcripts/                # output (gitignored)
└── INDEX.md                    # generated index (gitignored)
```

## Privacy

Your `transcripts/` folder and `INDEX.md` are **gitignored by default**. They contain meeting content and participant info — make sure you keep them out of any public repo.

## Credit / prior art

This builds on the work of others who reverse-engineered the Granola API:

- [getprobo/reverse-engineering-granola-api](https://github.com/getprobo/reverse-engineering-granola-api)
- [wassimk/granary](https://github.com/wassimk/granary)
- [magarcia/granola-cli](https://github.com/magarcia/granola-cli)

## Disclaimer

This uses an undocumented endpoint, not Granola's official API. It can break at any time. Use at your own risk and don't rely on it for anything mission-critical.
