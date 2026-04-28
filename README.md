# Granola Exporter

A tiny macOS desktop app that exports your [Granola](https://www.granola.ai/) meeting notes and transcripts to local Markdown files — sortable, indexable, and ready to feed to an AI assistant for content creation.

**No Granola Enterprise plan required.** Reads your Granola desktop app's local cache plus an undocumented endpoint to fetch transcripts.

## Features

- **One-click export** — Tkinter GUI, ships with system Python (no `pip install`).
- **Smart diffing** — auto-detects meetings you haven't exported yet and pre-ticks them.
- **Selectable** — pick which meetings to export, or hit Select All.
- **Sortable filenames** — `YYYY-MM-DD_HHMM_<slug>_<id>.md` sorts chronologically anywhere.
- **YAML frontmatter** — `id`, `title`, `date`, `participants`, `has_transcript`, `segment_count`. Easy to query.
- **Friendly speaker labels** — system audio → `**Them:**`, microphone → `**Me:**`.
- **Index file** — `INDEX.md` with a sorted Markdown table of every meeting.
- **Resumable** — re-running skips files already on disk.
- **CLI fallback** — `extract.py` for headless / scripted use.

## Requirements

- **macOS 11+** (uses the Granola desktop app's local cache at `~/Library/Application Support/Granola/`)
- **Granola desktop app** installed and signed in
- **Python 3.9+** with **tkinter**

> macOS 12.3+ no longer ships `python3` by default. If `python3 --version` errors out in Terminal, install Python first (see below).

## Install

### Step 1 — Make sure you have Python 3 + tkinter

In Terminal:
```bash
python3 --version
python3 -c "import tkinter; print('tkinter OK')"
```

If either errors, install Python:

- **Easiest (recommended):** Download the official installer from [python.org/downloads/macos](https://www.python.org/downloads/macos/). It includes tkinter out of the box.
- **Homebrew users:** `brew install python python-tk` (you need both — Homebrew's main Python package doesn't bundle tkinter).

### Step 2 — Get the app

**Option A — clone with git:**
```bash
git clone https://github.com/deewang/granola-exporter.git ~/Applications/GranolaExport
```

**Option B — download ZIP** (no git required):

1. Go to https://github.com/deewang/granola-exporter
2. Click the green **Code** button → **Download ZIP**
3. Unzip it and move the `granola-exporter-main` folder somewhere convenient (e.g. `~/Applications/GranolaExport`).

### Step 3 — First launch (one-time Gatekeeper step)

Because the app isn't code-signed by Apple, macOS will block it the first time you double-click. Two ways around it:

**The easy way** — in Finder, **right-click `Granola Export.app` → Open** → click **Open** in the dialog. macOS remembers your choice; future launches just work.

**The terminal way** — run once:
```bash
xattr -cr "/path/to/GranolaExport/Granola Export.app"
```

Then double-click as normal.

> Alternative: skip the `.app` bundle entirely and double-click `Granola Export.command`. It opens a small Terminal window and launches the same GUI — no Gatekeeper prompt.

### Step 4 — Add to Dock (optional)

Drag `Granola Export.app` from Finder onto your Dock for one-click access. Spotlight (`Cmd + Space` → "Granola Export") also picks it up.

## Usage

### GUI

Launch the app. It will:

1. Auto-load all meetings from your local Granola cache.
2. Mark anything not yet exported as 🆕 NEW (light-blue rows, pre-ticked).
3. Let you tick / untick rows; **Select New / Select All / Clear** for bulk control.
4. On **Export Selected**, fetch transcripts via the API, write `transcripts/*.md`, and update `INDEX.md`.

Output lands in `transcripts/` next to the app by default; pick any folder via the **…** button.

### CLI

```bash
cd ~/Applications/GranolaExport
python3 extract.py                  # all meetings, default output folder
python3 extract.py --limit 5        # test with 5
python3 extract.py --out ~/Notes    # custom output folder
python3 extract.py --force          # re-export existing files
python3 extract.py --no-transcripts # notes only, no API calls
```

## How it works

1. Reads your Granola auth token from `~/Library/Application Support/Granola/supabase.json` (prefers `workos_tokens`, falls back to legacy `cognito_tokens`).
2. Reads the meeting list from `~/Library/Application Support/Granola/cache-v6.json`.
3. For each meeting, POSTs to `https://api.granola.ai/v1/get-document-transcript` with `Authorization: Bearer <token>`.
4. Coalesces consecutive same-source segments into paragraphs, applies friendly speaker labels, and writes Markdown.

If the token expires (~6h), open the Granola app once to refresh it, then click **Refresh** in the GUI.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python3: command not found` when launching | Install Python — see Step 1. |
| `ModuleNotFoundError: No module named 'tkinter'` | Install tkinter (`brew install python-tk` if on Homebrew). |
| App won't open: "Apple could not verify…" | Right-click `Granola Export.app` → Open. Or use `Granola Export.command`. |
| `Auth required` after clicking Refresh | Open the Granola desktop app once (refreshes the token), then click Refresh again. |
| `cache-v6.json not found` | Make sure the Granola desktop app is installed and you've used it at least once. |
| Some meetings show `—` for Transcript | Granola only stores transcripts for meetings where recording happened. Old meetings with no recording return 404. |

## File layout

```
GranolaExport/
├── Granola Export.app          # macOS app bundle (double-clickable)
├── Granola Export.command      # alt launcher (opens in Terminal)
├── gui.py                      # Tkinter GUI
├── extract.py                  # CLI
├── granola_core.py             # shared logic (auth, API, file output)
├── migrate_speaker_labels.py   # one-shot script to update old exports
├── transcripts/                # output (gitignored — your data stays local)
└── INDEX.md                    # generated index (gitignored)
```

## Privacy

`transcripts/` and `INDEX.md` are **gitignored by default**. They contain meeting content and participant info — they will never be staged for commit when you push changes back to a fork.

## Credit / prior art

Builds on the work of others who reverse-engineered the Granola API:

- [getprobo/reverse-engineering-granola-api](https://github.com/getprobo/reverse-engineering-granola-api)
- [wassimk/granary](https://github.com/wassimk/granary)
- [magarcia/granola-cli](https://github.com/magarcia/granola-cli)

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This uses an undocumented endpoint, not Granola's official API. It can break at any time. Use at your own risk and don't rely on it for anything mission-critical.
