"""Shared logic for Granola transcript extraction (used by both CLI + GUI)."""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

GRANOLA_DIR = Path.home() / "Library/Application Support/Granola"
SUPABASE_FILE = GRANOLA_DIR / "supabase.json"
CACHE_FILE = GRANOLA_DIR / "cache-v6.json"

API_BASE = "https://api.granola.ai/v1"
CLIENT_VERSION = "5.354.0"

# Default output folder — sibling to this file.
DEFAULT_OUT_ROOT = Path(__file__).resolve().parent

# Speaker label rename: system audio = the other party, mic = the user.
SPEAKER_LABELS = {
    "system": "Them",
    "microphone": "Me",
}


def build_ssl_context() -> ssl.SSLContext:
    """macOS python.org builds ship without a CA bundle; use system or certifi."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for path in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl@3/cert.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


# ---------- auth ----------

class AuthError(Exception):
    pass


def load_access_token() -> tuple[str, str, int]:
    """Returns (token, source_name, seconds_remaining). Raises AuthError if none valid."""
    if not SUPABASE_FILE.exists():
        raise AuthError(f"{SUPABASE_FILE} not found — is Granola installed?")
    with open(SUPABASE_FILE) as f:
        outer = json.load(f)

    candidates = []
    if "workos_tokens" in outer:
        candidates.append(("workos_tokens", json.loads(outer["workos_tokens"])))
    if "cognito_tokens" in outer:
        candidates.append(("cognito_tokens", json.loads(outer["cognito_tokens"])))

    for name, bundle in candidates:
        token = bundle.get("access_token")
        if not token:
            continue
        obtained_at = bundle.get("obtained_at", 0)
        expires_in = bundle.get("expires_in", 0)
        remaining = 0
        if obtained_at and expires_in:
            obtained_s = obtained_at / 1000 if obtained_at > 10**12 else obtained_at
            remaining = int(obtained_s + expires_in - time.time())
            if remaining <= 0:
                continue
        return token, name, remaining

    raise AuthError("No valid access token found. Open the Granola app and sign in, then try again.")


# ---------- HTTP ----------

def post_json(url: str, body: dict, token: str, timeout: int = 30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"Granola/{CLIENT_VERSION}",
            "X-Client-Version": CLIENT_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        raw = resp.read()
        encoding = resp.headers.get("Content-Encoding", "").lower()
        if encoding == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def fetch_transcript(doc_id: str, token: str):
    try:
        return post_json(f"{API_BASE}/get-document-transcript", {"document_id": doc_id}, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------- documents ----------

def load_documents() -> list[dict]:
    """Returns list of valid meeting documents, newest first."""
    if not CACHE_FILE.exists():
        raise AuthError(f"{CACHE_FILE} not found — is Granola installed?")
    with open(CACHE_FILE) as f:
        state = json.load(f)["cache"]["state"]
    docs = list(state["documents"].values())
    # filter
    docs = [
        d for d in docs
        if not d.get("deleted_at")
        and d.get("type", "meeting") == "meeting"
    ]
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return docs


# ---------- formatting ----------

def slugify(text: str, max_len: int = 50) -> str:
    text = text.strip().lower() if text else "untitled"
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return (text or "untitled")[:max_len].rstrip("-")


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_people(doc: dict) -> list[str]:
    people = doc.get("people") or {}
    names = []
    if isinstance(people, dict):
        for group in people.values():
            if isinstance(group, list):
                for p in group:
                    if isinstance(p, dict):
                        n = p.get("name") or p.get("email")
                        if n:
                            names.append(n)
    return names


def meeting_filename(doc: dict) -> str:
    created = parse_iso(doc.get("created_at"))
    date_str = created.astimezone().strftime("%Y-%m-%d_%H%M") if created else "0000-00-00_0000"
    title = doc.get("title") or "Untitled"
    short_id = doc["id"][:8]
    return f"{date_str}_{slugify(title)}_{short_id}.md"


def coalesce_transcript(segments: list) -> str:
    """Merge consecutive same-source segments into paragraphs with friendly speaker labels."""
    if not segments:
        return ""
    lines = []
    current_source = None
    buf: list[str] = []
    for seg in segments:
        src = seg.get("source", "unknown")
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if src != current_source and buf:
            label = SPEAKER_LABELS.get(current_source, current_source.title())
            lines.append(f"**{label}:** " + " ".join(buf))
            buf = []
        current_source = src
        buf.append(text)
    if buf:
        label = SPEAKER_LABELS.get(current_source, current_source.title())
        lines.append(f"**{label}:** " + " ".join(buf))
    return "\n\n".join(lines)


def yaml_escape(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------- file output ----------

@dataclass
class MeetingMeta:
    filename: str
    title: str
    date: str
    participants: list[str]
    has_transcript: bool
    segments: int


def write_meeting_file(out_dir: Path, doc: dict, segments: Optional[list]) -> tuple[Path, MeetingMeta]:
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = meeting_filename(doc)
    path = out_dir / filename

    title = doc.get("title") or "Untitled"
    people = extract_people(doc)
    notes_md = (doc.get("notes_markdown") or "").strip()
    summary = (doc.get("summary") or "").strip() if isinstance(doc.get("summary"), str) else ""
    transcript_md = coalesce_transcript(segments) if segments else ""

    frontmatter = [
        "---",
        f"id: {doc['id']}",
        f"title: {yaml_escape(title)}",
        f"date: {doc.get('created_at', '')}",
        f"updated_at: {doc.get('updated_at', '')}",
        f"participants: [{', '.join(yaml_escape(p) for p in people)}]",
        f"has_transcript: {'true' if segments else 'false'}",
        f"segment_count: {len(segments) if segments else 0}",
        "---",
        "",
    ]
    parts = ["\n".join(frontmatter), f"# {title}\n"]
    if summary:
        parts += ["## Summary\n", summary, ""]
    if notes_md:
        parts += ["## Notes\n", notes_md, ""]
    if transcript_md:
        parts += ["## Transcript\n", transcript_md, ""]
    elif segments is None:
        parts += ["_No transcript available._\n"]

    path.write_text("\n".join(parts))
    return path, MeetingMeta(
        filename=filename,
        title=title,
        date=doc.get("created_at", ""),
        participants=people,
        has_transcript=bool(segments),
        segments=len(segments) if segments else 0,
    )


def write_index(out_root: Path, entries: list[MeetingMeta]):
    entries_sorted = sorted(entries, key=lambda e: e.date or "", reverse=True)
    lines = [
        "# Granola Meetings Index",
        "",
        f"Total meetings: **{len(entries_sorted)}**",
        f"With transcripts: **{sum(1 for e in entries_sorted if e.has_transcript)}**",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "| Date | Title | Participants | Transcript | File |",
        "| --- | --- | --- | --- | --- |",
    ]
    for e in entries_sorted:
        d = parse_iso(e.date)
        date_disp = d.astimezone().strftime("%Y-%m-%d %H:%M") if d else "—"
        people = ", ".join(e.participants[:4]) or "—"
        if len(e.participants) > 4:
            people += f" +{len(e.participants) - 4}"
        has_tx = "✅" if e.has_transcript else "—"
        link = f"[{e.filename}](transcripts/{e.filename})"
        title = e.title.replace("|", "\\|")
        lines.append(f"| {date_disp} | {title} | {people} | {has_tx} | {link} |")
    (out_root / "INDEX.md").write_text("\n".join(lines) + "\n")


def scan_existing(transcripts_dir: Path) -> set[str]:
    """Return set of filenames already on disk."""
    if not transcripts_dir.exists():
        return set()
    return {p.name for p in transcripts_dir.glob("*.md")}


def collect_existing_meta(transcripts_dir: Path) -> list[MeetingMeta]:
    """Best-effort parse of existing files for index regeneration."""
    out: list[MeetingMeta] = []
    if not transcripts_dir.exists():
        return out
    for p in transcripts_dir.glob("*.md"):
        try:
            text = p.read_text()
            fm = {}
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end > 0:
                    for line in text[3:end].splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            fm[k.strip()] = v.strip().strip('"')
            out.append(MeetingMeta(
                filename=p.name,
                title=fm.get("title", p.stem),
                date=fm.get("date", ""),
                participants=[],
                has_transcript=fm.get("has_transcript", "false") == "true",
                segments=int(fm.get("segment_count", 0) or 0),
            ))
        except Exception:
            pass
    return out
