"""Glue between capture → transcribe → write_meeting_file.

The orchestration:
  1. Take a RecordingSession that's already in 'done' state (system.wav + mic.wav on disk)
  2. Make sure the Whisper model exists (download if not)
  3. Transcribe both tracks, merge by timestamp
  4. Reshape into the same doc-dict + segments-list shape that
     granola_core.write_meeting_file expects
  5. Write the Markdown file + refresh INDEX.md
  6. Mark the session as done, clean up audio files if prefs say so
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from capture import RecordingSession, PENDING_DIR
from granola_core import (
    DEFAULT_OUT_ROOT,
    Preferences,
    collect_existing_meta,
    load_preferences,
    write_index,
    write_meeting_file,
)
from transcribe import (
    TranscribeError,
    download_model,
    find_whisper_cli,
    model_exists,
    transcribe_wav,
)


class WhisperCliMissing(Exception):
    """Raised when whisper-cli isn't installed. The recording's audio is
    preserved so it can be transcribed once whisper.cpp is available."""


def finalize_recording(session: RecordingSession,
                       title: Optional[str] = None,
                       log: Optional[Callable[[str], None]] = None) -> Path:
    """Transcribe + write a meeting file for a completed recording session.

    Returns the path to the new .md file.
    """
    prefs = load_preferences()
    model_size = getattr(prefs, "transcription_model", None) or "small.en"

    # Step 0 — bail early (and non-destructively) if whisper-cli is missing.
    # The audio stays on disk so the user can transcribe after installing.
    if find_whisper_cli() is None:
        try:
            status_path = session.out_dir / "status.json"
            if status_path.exists():
                data = json.loads(status_path.read_text())
                data["status"] = "needs_whisper"
                status_path.write_text(json.dumps(data))
        except Exception:
            pass
        raise WhisperCliMissing(
            f"whisper-cli isn't installed, so this recording couldn't be "
            f"transcribed yet. Your audio is saved at:\n{session.out_dir}\n\n"
            f"Install whisper.cpp, then it'll transcribe automatically next "
            f"time the app scans for pending recordings."
        )

    # Step 1 — ensure the Whisper model is downloaded.
    if not model_exists(model_size):
        if log:
            log(f"[pipeline] model {model_size} not present, downloading…")
        download_model(model_size,
                       on_progress=lambda d, t: log and t and log(
                           f"[pipeline] download {d * 100 // t}% ({d}/{t})") or None,
                       log=log)

    # Step 2 — transcribe both tracks if they exist.
    segments: list[dict] = []
    for path, source in [(session.system_wav, "system"),
                          (session.mic_wav, "microphone")]:
        if not path.exists() or path.stat().st_size < 1024:
            if log:
                log(f"[pipeline] skipping {path.name} (missing or empty)")
            continue
        try:
            tr = transcribe_wav(path, source, model_size=model_size, log=log)
            segments.extend(tr)
        except Exception as e:
            if log:
                log(f"[pipeline] transcribe error on {path.name}: {e}")

    # Step 3 — sort segments by start time so Me / Them interleave correctly.
    segments.sort(key=lambda s: s.get("start", 0))

    # Step 4 — build a doc dict that write_meeting_file can consume.
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": session.session_id,
        "title": title or f"Recording {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "created_at": datetime.fromtimestamp(session.started_at, timezone.utc).isoformat(),
        "updated_at": now,
        "people": {},
        "notes_markdown": "",
        "summary": None,
    }

    # Step 5 — pick output folder.
    out_root = Path(prefs.output_folder) if prefs.output_folder else DEFAULT_OUT_ROOT
    out_dir = out_root / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)

    path, meta = write_meeting_file(out_dir, doc, segments)
    if log:
        log(f"[pipeline] wrote {path.name} ({len(segments)} segments)")

    # Step 6 — refresh INDEX.md with this new entry merged into existing.
    try:
        entries = collect_existing_meta(out_dir)
        write_index(out_root, entries)
        if log:
            log(f"[pipeline] refreshed INDEX.md")
    except Exception as e:
        if log:
            log(f"[pipeline] INDEX.md refresh failed: {e}")

    # Step 7 — bookkeeping. Mark session done; clean up audio unless requested.
    try:
        status_path = session.out_dir / "status.json"
        if status_path.exists():
            data = json.loads(status_path.read_text())
            data["status"] = "done"
            data["finalized_at"] = time.time()
            data["output_path"] = str(path)
            status_path.write_text(json.dumps(data))
    except Exception:
        pass

    if not getattr(prefs, "keep_raw_audio", False):
        try:
            shutil.rmtree(session.out_dir)
            if log:
                log(f"[pipeline] cleaned up {session.out_dir}")
        except Exception as e:
            if log:
                log(f"[pipeline] cleanup failed: {e}")

    return path


def find_pending_sessions() -> list[Path]:
    """Return a list of session dirs that have audio but haven't been finalised."""
    if not PENDING_DIR.exists():
        return []
    pending: list[Path] = []
    for entry in PENDING_DIR.iterdir():
        if not entry.is_dir():
            continue
        status_path = entry / "status.json"
        if not status_path.exists():
            continue
        try:
            data = json.loads(status_path.read_text())
        except Exception:
            continue
        # 'recording' = crashed mid-session; 'needs_whisper' = captured but
        # whisper-cli wasn't installed at the time.
        if data.get("status") in ("pending", "recording", "needs_whisper"):
            pending.append(entry)
    return pending
