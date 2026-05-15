"""Python orchestrator for the Swift recorder CLI.

Spawns `recorder/recorder <out_dir>` as a subprocess, reads its JSON events
from stdout, and exposes a clean RecordingSession API to the GUI.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Locate the recorder binary across (1) source-tree, (2) PyInstaller _MEIPASS,
# (3) bundle Resources directory. Mirrors the menubar-icon lookup pattern.
def find_recorder_binary() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    candidates: list[Path] = [
        here / "recorder" / "recorder",
        here / "recorder",
    ]
    if getattr(sys, "_MEIPASS", ""):
        meipass = Path(sys._MEIPASS)
        candidates.append(meipass / "recorder" / "recorder")
        candidates.append(meipass / "recorder")
    if getattr(sys, "frozen", False) and sys.executable:
        # Bundle: .../Granola Export.app/Contents/MacOS/Granola Export
        contents = Path(sys.executable).parent.parent
        candidates.append(contents / "Resources" / "recorder" / "recorder")
        candidates.append(contents / "Resources" / "recorder")
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


# Where in-progress recordings land — separate from finalised .md output so
# crash recovery can find orphans.
PENDING_DIR = Path.home() / "Library/Application Support/GranolaExport/pending"


@dataclass
class RecordingSession:
    session_id: str
    out_dir: Path
    started_at: float
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    state: str = "idle"            # idle | starting | recording | stopping | done | error
    last_error: str = ""
    on_state_change: Optional[Callable[[str], None]] = field(default=None, repr=False)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.state == "recording" else 0.0

    @property
    def system_wav(self) -> Path:
        return self.out_dir / "system.wav"

    @property
    def mic_wav(self) -> Path:
        return self.out_dir / "mic.wav"

    def _set_state(self, new_state: str):
        self.state = new_state
        if self.on_state_change:
            try:
                self.on_state_change(new_state)
            except Exception:
                pass


# Module-level singleton — only one recording at a time.
_lock = threading.Lock()
_active: Optional[RecordingSession] = None


class RecordingError(Exception):
    pass


def get_active_session() -> Optional[RecordingSession]:
    return _active


def start_recording(on_state_change: Optional[Callable[[str], None]] = None,
                    log: Optional[Callable[[str], None]] = None) -> RecordingSession:
    """Start a new recording. Returns the RecordingSession.

    Raises RecordingError if one is already in progress or the recorder
    binary can't be found.
    """
    global _active
    with _lock:
        if _active is not None and _active.state in ("starting", "recording", "stopping"):
            raise RecordingError(f"A recording is already in progress ({_active.state})")

        binary = find_recorder_binary()
        if binary is None:
            raise RecordingError(
                "recorder binary not found. Run ./recorder/build-recorder.sh first."
            )

        session_id = uuid.uuid4().hex[:12]
        out_dir = PENDING_DIR / session_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # Status sentinel so we can recover orphans on app crash.
        (out_dir / "status.json").write_text(json.dumps({
            "session_id": session_id,
            "status": "recording",
            "started_at": time.time(),
        }))

        session = RecordingSession(
            session_id=session_id,
            out_dir=out_dir,
            started_at=time.time(),
            on_state_change=on_state_change,
        )

        if log:
            log(f"[capture] launching {binary} {out_dir}")

        proc = subprocess.Popen(
            [str(binary), str(out_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )
        session.process = proc
        session._set_state("starting")

        # Read JSON events from stdout in a background thread.
        def reader():
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    if log:
                        log(f"[recorder raw] {line}")
                    continue
                ev = event.get("event", "")
                if ev == "recording_started":
                    session._set_state("recording")
                elif ev == "shutdown_complete":
                    session._set_state("done")
                elif ev == "error":
                    session.last_error = event.get("message", "")
                    if log:
                        log(f"[recorder ERROR] {session.last_error}")
                    # Don't immediately fail — recorder may continue with one track.
                if log and ev not in ("tick",):
                    log(f"[recorder] {ev}: {json.dumps({k: v for k, v in event.items() if k not in ('event', 'ts')})}")
            # Stream closed
            rc = proc.wait()
            if log:
                log(f"[capture] recorder exited rc={rc}")
            if session.state in ("starting", "recording", "stopping"):
                if rc == 0:
                    session._set_state("done")
                else:
                    session.last_error = session.last_error or f"recorder exited with code {rc}"
                    session._set_state("error")

        session._reader_thread = threading.Thread(target=reader, daemon=True)
        session._reader_thread.start()

        _active = session
        return session


def stop_recording(timeout: float = 5.0,
                   log: Optional[Callable[[str], None]] = None) -> Optional[RecordingSession]:
    """Send SIGINT to the active recorder + wait for it to exit cleanly.

    Returns the session (now in 'done' or 'error' state), or None if none was
    active.
    """
    global _active
    with _lock:
        session = _active
        if session is None or session.process is None:
            return None

        session._set_state("stopping")
        if log:
            log(f"[capture] sending SIGINT to recorder pid={session.process.pid}")
        try:
            session.process.send_signal(signal.SIGINT)
        except Exception as e:
            if log:
                log(f"[capture] SIGINT failed: {e}")

        # Wait for the reader thread to drain the pipe + process to exit.
        try:
            session.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if log:
                log(f"[capture] recorder didn't exit in {timeout}s — killing")
            session.process.kill()
            session.process.wait()

        if session._reader_thread:
            session._reader_thread.join(timeout=2.0)

        # Update status sentinel.
        status_path = session.out_dir / "status.json"
        try:
            data = json.loads(status_path.read_text()) if status_path.exists() else {}
            data["status"] = "pending" if session.state == "done" else "failed"
            data["stopped_at"] = time.time()
            if session.last_error:
                data["error"] = session.last_error
            status_path.write_text(json.dumps(data))
        except Exception:
            pass

        _active = None
        return session
