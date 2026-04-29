# Granola Exporter

A modern macOS desktop app that exports your [Granola](https://www.granola.ai/) meeting notes and transcripts to local Markdown files — sortable, indexable, viewable in-app, and ready to feed to an AI assistant for content creation.

**No Granola Enterprise plan required.** Reads your Granola desktop app's local cache plus an undocumented endpoint to fetch transcripts.

## Features

- **Native-feeling macOS GUI** — warm light theme inspired by Granola, rounded controls, hover states.
- **Smart diffing** — auto-detects meetings you haven't exported yet and pre-ticks them with a 🆕 NEW pill.
- **Click any meeting to view it** — opens a detail window that renders the Markdown with proper headings, `**Me:**` in green, `**Them:**` in violet. Works for already-exported meetings (instant, local) or fetches on-demand for new ones.
- **Pagination + sort** — 50 per page, sort by date / title / status. Selection persists across pages.
- **Connection status chip** — shows live token state (Connected · 5h 23m / 12 min left / Session expired). Click to reconnect.
- **Auto-reconnect flow** — opens the Granola app and watches `supabase.json` for the new session. No "open Granola, re-run script" dance.
- **Sortable filenames** — `YYYY-MM-DD_HHMM_<slug>_<id>.md` sorts chronologically anywhere.
- **YAML frontmatter** — `id`, `title`, `date`, `participants`, `has_transcript`, `segment_count`. Easy to query.
- **Friendly speaker labels** — system audio → `**Them:**`, microphone → `**Me:**`.
- **`INDEX.md`** — generated sorted Markdown table of every meeting.
- **Resumable** — re-running skips files already on disk.
- **CLI fallback** — `extract.py` for headless / scripted use.
- **Self-contained `.app`** — Python and Tkinter bundled. End users don't install anything.

## Requirements

- **macOS 11+** (uses the Granola desktop app's local cache at `~/Library/Application Support/Granola/`)
- **Granola desktop app** installed and signed in

The pre-built `.app` is fully self-contained — **no Python install required**. (If you'd rather run from source, see [Building from source](#building-from-source).)

## Install (end users)

### Step 1 — Download

Grab the latest `Granola-Export-X.Y.dmg` from the [Releases page](https://github.com/deewang/granola-exporter/releases).

### Step 2 — Install

1. Double-click the DMG.
2. Drag **Granola Export** onto the **Applications** shortcut.
3. **Eject the DMG** before launching. (Running directly from the mounted DMG fails — DMGs are read-only.)

### Step 3 — First launch (one-time Gatekeeper step)

Because the app isn't yet code-signed by Apple, macOS will block it the first time you launch:

> "Apple could not verify 'Granola Export' is free of malware…"

In Finder, **right-click `Granola Export` in Applications → Open** → click **Open** in the dialog. macOS remembers your choice; future launches just work.

### Step 4 — Add to Dock (optional)

Drag `Granola Export` from Applications onto your Dock for one-click access. Spotlight (`Cmd + Space` → "Granola Export") also picks it up.

## Usage

### GUI

Launch the app. It auto-loads on start:

1. **Lists all your meetings** (newest first by default), marking unexported ones with a 🆕 NEW pill.
2. **Pre-ticks new meetings** so you can immediately hit Export.
3. **Click any row body** → opens a detail window with the full transcript and notes.
4. **Click a checkbox** → toggles selection (matches Mail.app pattern).
5. **Sort** by date / title / status via the dropdown above the list.
6. **Paginate** with Prev / Next / First / Last. Selection persists across pages.
7. **Export Selected** → fetches transcripts via the API, writes `transcripts/*.md`, updates `INDEX.md`, flips NEW → EXPORTED pills live.

**Output folder:** defaults to `~/Documents/Granola Export/transcripts/`. The current path is shown in the "Saving to:" label under the toolbar — pick any folder via **Choose…**.

### Connection status

The chip in the top-right shows your Granola session state:

| Color | Meaning |
|---|---|
| 🟢 **Connected · 5h 23m** | Token valid, app ready |
| 🟡 **12 min left** | Token expires soon |
| 🔴 **Session expired** | Click chip → Reconnect → app opens Granola → auto-detects new session |

Tokens last ~6 hours. The reconnect flow opens Granola for you and watches `supabase.json` for up to 2 minutes for the refreshed session.

### CLI

```bash
cd /Applications/Granola\ Export.app/Contents/MacOS  # or your source dir
python3 extract.py                  # all meetings, default output folder
python3 extract.py --limit 5        # test with 5
python3 extract.py --out ~/Notes    # custom output folder
python3 extract.py --force          # re-export existing files
python3 extract.py --no-transcripts # notes only, no API calls
```

The CLI is most useful when running from a source clone. To run from a clone:

```bash
git clone https://github.com/deewang/granola-exporter.git
cd granola-exporter
python3 extract.py --limit 5
```

## How it works

| Data | Source | When |
|---|---|---|
| **Meeting list** (titles, dates, participants) | Local: `~/Library/Application Support/Granola/cache-v6.json` | On Refresh — instant, offline-capable |
| **Auth token** | Local: `~/Library/Application Support/Granola/supabase.json` (prefers `workos_tokens`, falls back to legacy `cognito_tokens`) | On Refresh / Reconnect |
| **Transcript text** | Cloud: `POST https://api.granola.ai/v1/get-document-transcript` with `Authorization: Bearer <token>` | Only when you Export — requires internet |

For each exported meeting the app coalesces consecutive same-source transcript segments into paragraphs, applies the `Me:` / `Them:` labels, prepends YAML frontmatter, and writes Markdown.

The app also includes an in-window **"ⓘ Where does this data come from?"** link that shows users this same explanation.

## Limitations

- **Internet required** at export time. The list itself works offline.
- **Meetings without recordings return 404** — only their notes export, no transcript section.
- **Old transcripts may be deleted** by Granola for retention/privacy. Once gone from their cloud, gone for us. Export now to preserve.
- **Undocumented endpoint** — Granola could change or remove the transcript API at any time.

## Privacy

The only network hop is HTTPS between Granola's cloud and this app on your Mac. Markdown files stay local. `transcripts/` and `INDEX.md` are gitignored by default — they will never be staged for commit if you fork the repo.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App won't open: "Apple could not verify…" | Right-click `Granola Export.app` → Open (one-time only). |
| `OSError: Read-only file system` on export | You're running directly from the DMG. Drag the app to Applications, eject the DMG, then launch from there. |
| Connection chip shows red "Session expired" | Click the chip → Reconnect → finish signing in inside the Granola app that opens. |
| `cache-v6.json not found` | Make sure the Granola desktop app is installed and you've launched it at least once. |
| Some meetings show "no transcript" | Granola only stores transcripts for meetings where recording happened. Old / no-record meetings return 404. |
| Detail-window fetch seems stuck | Long meetings (1000+ segments) take a few seconds. The button pulses while fetching. The log panel (bottom-right "Show log") shows per-step progress. |

## Building from source

If you've cloned the repo and want to rebuild the `.app`:

```bash
# 1. Install Python 3.9+ from https://python.org/downloads/macos/
#    (Includes tkinter out of the box. Homebrew Python users also need: brew install python-tk)
# 2. Install runtime dependencies
python3 -m pip install --user customtkinter

# 3. Build the .app  (~30 MB self-contained bundle)
./build.sh                       # → dist/Granola Export.app

# 4. (optional) Package as drag-to-install DMG  (~13 MB)
VERSION=1.6 ./make-dmg.sh        # → Granola-Export-1.6.dmg
```

The build uses [PyInstaller](https://pyinstaller.org/) to bundle Python, Tkinter, CustomTkinter, and all sources into a single `.app`. Build outputs are gitignored.

You can also run from source directly without building:

```bash
python3 gui.py        # GUI
python3 extract.py    # CLI
```

## Distribution / commercial use

This repo ships an unsigned `.app`. To remove the Gatekeeper warning and ship commercially:

1. **Apple Developer Program** ($99/year) — required for code signing and notarization.
2. **Code sign** the bundle:
   ```bash
   codesign --deep --force --options runtime \
     --sign "Developer ID Application: Your Name (TEAMID)" \
     "dist/Granola Export.app"
   ```
3. **Notarize** with Apple (one-time per build):
   ```bash
   xcrun notarytool submit Granola-Export-1.6.dmg \
     --apple-id you@example.com --team-id TEAMID --password APP_PASSWORD --wait
   xcrun stapler staple Granola-Export-1.6.dmg
   ```
4. **Auto-updates** (optional): integrate [Sparkle](https://sparkle-project.org/).
5. **Sales / licensing**: [Gumroad](https://gumroad.com), [Paddle](https://paddle.com), or Stripe + a license-key check inside the app.

The current MIT license permits commercial use, but if you ship paid you'll likely want to add a EULA and a license-key gate.

## File layout

```
granola-exporter/
├── Granola Export.app          # source-tree launcher (uses system Python)
├── Granola Export.command      # alt source-tree launcher (Terminal-based)
├── gui.py                      # CustomTkinter GUI
├── extract.py                  # CLI
├── granola_core.py             # shared logic (auth, API, Markdown writer)
├── migrate_speaker_labels.py   # one-shot script to update old exports
├── build.sh                    # build self-contained .app via PyInstaller
├── make-dmg.sh                 # package .app as drag-to-install DMG
├── README.md, LICENSE, .gitignore
├── transcripts/                # output (gitignored — your data stays local)
├── INDEX.md                    # generated index (gitignored)
├── dist/                       # PyInstaller output (gitignored)
└── *.dmg                       # release artefacts (gitignored)
```

## Credit / prior art

Builds on the work of others who reverse-engineered the Granola API:

- [getprobo/reverse-engineering-granola-api](https://github.com/getprobo/reverse-engineering-granola-api)
- [wassimk/granary](https://github.com/wassimk/granary)
- [magarcia/granola-cli](https://github.com/magarcia/granola-cli)

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This uses an undocumented endpoint, not Granola's official API. It can break at any time. Use at your own risk and don't rely on it for anything mission-critical. The author has no affiliation with Granola Labs.
