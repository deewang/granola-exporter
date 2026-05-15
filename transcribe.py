"""Local Whisper transcription via the whisper.cpp `whisper-cli` binary.

`whisper-cli -oj -f input.wav -m model.bin` prints JSON to stdout containing
segments. We parse that and shape it into the dict-list format that the
existing `coalesce_transcript()` in granola_core consumes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional


# Where the downloaded ggml-*.bin models live.
MODEL_DIR = Path.home() / "Library/Application Support/GranolaExport/whisper-models"

# Hugging Face mirror of all the standard quantised whisper.cpp models.
HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

# Map our friendly size names to ggml filenames.
MODEL_FILES = {
    "tiny.en":    "ggml-tiny.en.bin",
    "base.en":    "ggml-base.en.bin",
    "small.en":   "ggml-small.en.bin",
    "medium.en":  "ggml-medium.en.bin",
    "large-v3":   "ggml-large-v3.bin",
}


class TranscribeError(Exception):
    pass


def find_whisper_cli() -> Optional[str]:
    """Locate the whisper-cli binary. First commit: prefer a Homebrew install
    (`brew install whisper-cpp`). Future commits: bundle our own."""
    # 1) PATH lookup — finds Homebrew installs at /opt/homebrew/bin/whisper-cli
    found = shutil.which("whisper-cli")
    if found:
        return found
    # 2) Common Homebrew locations
    for p in ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"):
        if Path(p).exists() and os.access(p, os.X_OK):
            return p
    # 3) Bundled (future): _MEIPASS or Resources
    if getattr(sys, "_MEIPASS", ""):
        candidate = Path(sys._MEIPASS) / "whisper" / "whisper-cli"
        if candidate.exists():
            return str(candidate)
    if getattr(sys, "frozen", False) and sys.executable:
        contents = Path(sys.executable).parent.parent
        candidate = contents / "Resources" / "whisper" / "whisper-cli"
        if candidate.exists():
            return str(candidate)
    return None


def model_path(size: str) -> Path:
    """Path where the chosen model SHOULD live (may not exist yet)."""
    filename = MODEL_FILES.get(size, f"ggml-{size}.bin")
    return MODEL_DIR / filename


def model_exists(size: str) -> bool:
    return model_path(size).exists()


def download_model(size: str,
                   on_progress: Optional[Callable[[int, int], None]] = None,
                   log: Optional[Callable[[str], None]] = None) -> Path:
    """Download the chosen ggml model from Hugging Face. Streams to disk so
    we don't load it entirely into memory. Returns the local path."""
    filename = MODEL_FILES.get(size)
    if not filename:
        raise TranscribeError(f"unknown model size: {size}")

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{HF_BASE}/{filename}"
    dest = MODEL_DIR / filename
    tmp = dest.with_suffix(dest.suffix + ".part")

    if log:
        log(f"[whisper] downloading {url} → {dest}")

    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "GranolaExport/1.x"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", "0"))
        downloaded = 0
        chunk = 1 << 20  # 1 MiB
        with open(tmp, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if on_progress:
                    try:
                        on_progress(downloaded, total)
                    except Exception:
                        pass
    tmp.rename(dest)
    if log:
        log(f"[whisper] saved {dest} ({dest.stat().st_size} bytes)")
    return dest


def transcribe_wav(wav_path: Path,
                   source: str,
                   model_size: str = "small.en",
                   log: Optional[Callable[[str], None]] = None) -> list[dict]:
    """Run whisper-cli on a single WAV file and return segments tagged with
    the given source ('system' or 'microphone'). Empty list if the file has
    no audio."""
    if not wav_path.exists():
        if log:
            log(f"[whisper] {wav_path} doesn't exist, skipping")
        return []
    if wav_path.stat().st_size < 1024:
        if log:
            log(f"[whisper] {wav_path} is too small, skipping")
        return []

    cli = find_whisper_cli()
    if cli is None:
        raise TranscribeError(
            "whisper-cli not found. Install via `brew install whisper-cpp` "
            "or run the build pipeline to bundle it."
        )

    if not model_exists(model_size):
        raise TranscribeError(
            f"Model {model_size} not downloaded yet. "
            f"Call download_model('{model_size}') first."
        )

    cmd = [
        cli,
        "-m", str(model_path(model_size)),
        "-f", str(wav_path),
        "-oj",                    # JSON output
        "-of", str(wav_path.with_suffix("")),  # output file prefix (auto-append .json)
        "--no-prints",            # quieter
    ]
    if log:
        log(f"[whisper] {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise TranscribeError(
            f"whisper-cli exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )

    # whisper-cli writes <wav_path stem>.json
    json_path = wav_path.with_suffix(".json")
    if not json_path.exists():
        if log:
            log(f"[whisper] WARNING: expected {json_path} but it's missing")
        return []

    data = json.loads(json_path.read_text())
    segments_raw = data.get("transcription", [])
    segments: list[dict] = []
    for seg in segments_raw:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Whisper times are in centiseconds in the JSON. The newer schema uses
        # 'offsets': {'from': ms, 'to': ms}. Handle both.
        offsets = seg.get("offsets") or {}
        start_ms = offsets.get("from")
        end_ms = offsets.get("to")
        if start_ms is None:
            start_ms = (seg.get("from") or 0) * 10  # centiseconds → ms
        if end_ms is None:
            end_ms = (seg.get("to") or 0) * 10
        segments.append({
            "source": source,
            "text": text,
            "start": start_ms / 1000.0,
            "end": end_ms / 1000.0,
            "confidence": seg.get("confidence", 1.0),
        })
    if log:
        log(f"[whisper] {wav_path.name} → {len(segments)} segments")
    return segments
