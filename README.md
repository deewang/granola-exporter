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

The pre-built `.app` is fully self-contained — **no Python install required**. (Python and Tkinter are bundled inside the app.) If you'd rather run from source, see [Building from source](#building-from-source).

## Install (end users)

### Step 1 — Download

Grab the latest `Granola-Export-X.Y.dmg` from the [Releases page](https://github.com/deewang/granola-exporter/releases).

### Step 2 — Install

1. Double-click the DMG.
2. Drag **Granola Export** onto the **Applications** shortcut.
3. Eject the DMG.

### Step 3 — First launch (one-time Gatekeeper step)

Because the app isn't yet code-signed by Apple, macOS will block it the first time you launch:

> "Apple could not verify 'Granola Export' is free of malware…"

In Finder, **right-click `Granola Export` in Applications → Open** → click **Open** in the dialog. macOS remembers your choice; future launches work normally.

### Step 4 — Add to Dock (optional)

Drag `Granola Export` from Applications onto your Dock for one-click access. Spotlight (`Cmd + Space` → "Granola Export") also picks it up.

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
| App won't open: "Apple could not verify…" | Right-click `Granola Export.app` → Open (one-time only). |
| `Auth required` after clicking Refresh | Open the Granola desktop app once (refreshes the token), then click Refresh again. |
| `cache-v6.json not found` | Make sure the Granola desktop app is installed and you've used it at least once. |
| Some meetings show `—` for Transcript | Granola only stores transcripts for meetings where recording happened. Old meetings with no recording return 404. |

## Building from source

If you've cloned the repo and want to rebuild the `.app`:

```bash
# 1. Install Python 3.9+ from https://python.org/downloads/macos/
# 2. Build the .app
./build.sh                       # → dist/Granola Export.app  (~30 MB)

# 3. (optional) Package as drag-to-install DMG
./make-dmg.sh                    # → Granola-Export-1.0.dmg   (~13 MB)
```

The build uses [PyInstaller](https://pyinstaller.org/) to bundle Python, Tkinter, and all sources into a single `.app`. Output is gitignored.

You can also run from source directly without building:
```bash
python3 gui.py        # GUI
python3 extract.py    # CLI
```

## Distribution / commercial use

This repo ships an unsigned `.app`. To remove the Gatekeeper warning and ship commercially, you'll need:

1. **Apple Developer Program** ($99/year) — required for code signing and notarization.
2. **Code sign** the bundle:
   ```bash
   codesign --deep --force --options runtime \
     --sign "Developer ID Application: Your Name (TEAMID)" \
     "dist/Granola Export.app"
   ```
3. **Notarize** with Apple (one-time per build):
   ```bash
   xcrun notarytool submit Granola-Export-1.0.dmg \
     --apple-id you@example.com --team-id TEAMID --password APP_PASSWORD --wait
   xcrun stapler staple Granola-Export-1.0.dmg
   ```
4. **Auto-updates** (optional): integrate [Sparkle](https://sparkle-project.org/).
5. **Sales / licensing**: [Gumroad](https://gumroad.com), [Paddle](https://paddle.com), or Stripe + a license-key check inside the app.

The current MIT license permits commercial use, but if you're shipping paid you'll likely want to add a EULA and a license-key gate.

## File layout

```
GranolaExport/
├── Granola Export.app          # source-tree launcher (uses system Python)
├── Granola Export.command      # alt source-tree launcher
├── gui.py                      # Tkinter GUI
├── extract.py                  # CLI
├── granola_core.py             # shared logic (auth, API, file output)
├── migrate_speaker_labels.py   # one-shot script to update old exports
├── build.sh                    # build self-contained .app via PyInstaller
├── make-dmg.sh                 # package .app as drag-to-install DMG
├── transcripts/                # output (gitignored — your data stays local)
├── INDEX.md                    # generated index (gitignored)
├── dist/                       # PyInstaller output (gitignored)
└── *.dmg                       # release artefacts (gitignored)
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
