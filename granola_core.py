"""Shared logic for Granola transcript extraction (used by both CLI + GUI)."""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


# ---------- preferences ----------

PREFS_DIR = Path.home() / "Library/Application Support/GranolaExport"
PREFS_FILE = PREFS_DIR / "preferences.json"


@dataclass
class Preferences:
    auto_scan_enabled: bool = False                # in-process scan while app is open
    background_scan_enabled: bool = False          # launchd-driven scan even when app closed
    auto_scan_interval_minutes: int = 120          # 2 hours (used for both modes)
    notify_on_new: bool = True
    last_scan_iso: str = ""
    last_scan_new_count: int = 0
    last_scan_fetched_count: int = 0
    output_folder: str = ""                        # empty → use DEFAULT_OUT_ROOT
    # Auth-status tracking (used to fire one-shot notification on transition)
    # Values: "unknown" | "ok" | "expired"
    last_scan_auth_status: str = "unknown"
    # Whether the user has already seen the "we're now in the menu bar" hint
    # the first time they close the window with the red dot.
    shown_close_hint: bool = False


def load_preferences() -> "Preferences":
    if not PREFS_FILE.exists():
        return Preferences()
    try:
        raw = json.loads(PREFS_FILE.read_text())
        valid_keys = {f.name for f in Preferences.__dataclass_fields__.values()} \
            if hasattr(Preferences, "__dataclass_fields__") else set()
        # __dataclass_fields__ values are Field objects; iterate keys
        valid_keys = set(Preferences.__dataclass_fields__.keys())
        return Preferences(**{k: v for k, v in raw.items() if k in valid_keys})
    except Exception:
        return Preferences()


def save_preferences(prefs: "Preferences") -> None:
    from dataclasses import asdict
    PREFS_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(asdict(prefs), indent=2))


# ---------- macOS notifications ----------

def notify_macos(title: str, message: str, subtitle: str = "") -> bool:
    """Send a native macOS notification via osascript. Returns True on success."""
    import subprocess
    parts = [
        "display notification",
        json.dumps(message),
        f"with title {json.dumps(title)}",
    ]
    if subtitle:
        parts.append(f"subtitle {json.dumps(subtitle)}")
    script = " ".join(parts)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False, capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


# ---------- launchd background-scan agent ----------

LAUNCH_AGENT_LABEL = "com.davidwang.granolaexport.scanner"
LAUNCH_AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
DAEMON_LOG_PATH = Path.home() / "Library/Logs/GranolaExport-daemon.log"


def find_app_executable() -> Optional[Path]:
    """Find the bundled .app's main executable so launchd can call it.

    Order: current sys.executable (when running bundled), /Applications,
    ~/Applications.
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False) and sys.executable:
        candidates.append(Path(sys.executable))
    candidates.extend([
        Path("/Applications/Granola Export.app/Contents/MacOS/Granola Export"),
        Path.home() / "Applications/Granola Export.app/Contents/MacOS/Granola Export",
    ])
    for p in candidates:
        if p.exists():
            return p
    return None


def write_launch_agent_plist(executable: Path, interval_minutes: int) -> None:
    """Write the LaunchAgent plist that runs `<exe> --scan-once` on a schedule."""
    seconds = max(60, interval_minutes * 60)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>--scan-once</string>
    </array>
    <key>StartInterval</key>
    <integer>{seconds}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{DAEMON_LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>{DAEMON_LOG_PATH}</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.write_text(content)


def install_launch_agent(interval_minutes: int) -> tuple[bool, str]:
    """Write the plist and load it via launchctl. Returns (ok, message)."""
    import subprocess
    exe = find_app_executable()
    if not exe:
        return False, "Couldn't find Granola Export.app — install it to /Applications first."
    try:
        write_launch_agent_plist(exe, interval_minutes)
    except Exception as e:
        return False, f"Couldn't write LaunchAgent plist: {e}"

    uid = os.getuid()
    domain = f"gui/{uid}"
    # Unload existing first (idempotent)
    subprocess.run(
        ["launchctl", "bootout", domain, str(LAUNCH_AGENT_PATH)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(LAUNCH_AGENT_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}"
    return True, f"Background scan installed — every {interval_minutes} min"


def uninstall_launch_agent() -> tuple[bool, str]:
    import subprocess
    if LAUNCH_AGENT_PATH.exists():
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", str(LAUNCH_AGENT_PATH)],
            capture_output=True,
        )
        try:
            LAUNCH_AGENT_PATH.unlink()
        except Exception as e:
            return False, f"Removed from launchd but couldn't delete plist: {e}"
    return True, "Background scan removed"


def is_launch_agent_installed() -> bool:
    return LAUNCH_AGENT_PATH.exists()


# ---------- auth-status transition helper ----------

def maybe_notify_auth_expired(prefs: "Preferences") -> bool:
    """Fire a 'session expired' notification only when the auth status
    transitions from 'ok' (or the very first time) to 'expired'.

    Returns True if a notification was fired. Mutates prefs.
    """
    fire = prefs.last_scan_auth_status != "expired"
    prefs.last_scan_auth_status = "expired"
    if fire:
        notify_macos(
            title="Granola Export",
            message="Your Granola session has expired. Open the Granola app to sign in again, then auto-scan will resume.",
            subtitle="Auto-scan paused",
        )
    return fire


def mark_auth_ok(prefs: "Preferences") -> None:
    """Call after a successful authenticated operation."""
    prefs.last_scan_auth_status = "ok"


# ---------- headless scan (called from daemon via --scan-once) ----------

def run_scan_once() -> int:
    """Run a single scan + export pass, no UI, no event loop.

    Returns the number of newly-exported meetings (0 if nothing new, -1 on
    auth/cache failure). Sends a macOS notification for any new meetings.
    """
    prefs = load_preferences()
    if not prefs.auto_scan_enabled:
        # User disabled scanning entirely — daemon should be a no-op.
        return 0

    out_root = Path(prefs.output_folder) if prefs.output_folder else DEFAULT_OUT_ROOT
    out_dir = out_root / "transcripts"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return -1

    # 1) Auth — fire a one-shot notification on first failure
    try:
        token, _, _ = load_access_token()
        mark_auth_ok(prefs)
    except AuthError:
        maybe_notify_auth_expired(prefs)
        save_preferences(prefs)
        return -1

    # 2) Cache
    try:
        docs = load_documents()
    except Exception:
        save_preferences(prefs)
        return -1

    existing = scan_existing(out_dir)
    new_docs = [d for d in docs if meeting_filename(d) not in existing]

    # Persist last-scan timestamp regardless of result
    prefs.last_scan_iso = datetime.now().isoformat(timespec="seconds")

    if not new_docs:
        prefs.last_scan_new_count = 0
        prefs.last_scan_fetched_count = 0
        save_preferences(prefs)
        return 0

    # 3) Fetch + write
    entries_by_name = {m.filename: m for m in collect_existing_meta(out_dir)}
    fetched = errors = 0
    for doc in new_docs:
        segments = None
        try:
            resp = fetch_transcript(doc["id"], token)
            if isinstance(resp, list):
                segments = resp
                if segments:
                    fetched += 1
            elif isinstance(resp, dict):
                for k in ("transcript", "segments", "data"):
                    if isinstance(resp.get(k), list):
                        segments = resp[k]
                        if segments:
                            fetched += 1
                        break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Token expired mid-scan — fire transition notification + bail
                maybe_notify_auth_expired(prefs)
                save_preferences(prefs)
                return -1
            errors += 1
            continue
        except Exception:
            errors += 1
            continue
        try:
            _, meta = write_meeting_file(out_dir, doc, segments)
            entries_by_name[meta.filename] = meta
        except Exception:
            errors += 1

    try:
        write_index(out_root, list(entries_by_name.values()))
    except Exception:
        pass

    prefs.last_scan_new_count = len(new_docs)
    prefs.last_scan_fetched_count = fetched
    save_preferences(prefs)

    if prefs.notify_on_new:
        n = len(new_docs)
        phrase = "a new meeting has" if n == 1 else f"{n} new meetings have"
        notify_macos(
            title="Granola Export",
            message=f"Hey, {phrase} been detected, and the transcription has been exported to your computer.",
            subtitle=f"{fetched} with transcripts" if fetched else "Notes only",
        )
    return len(new_docs)


def _read_version() -> str:
    """Single source of truth for the app version, read from VERSION file.

    Works in both source-tree mode and PyInstaller-bundled mode (the build
    script copies VERSION into the bundle's Resources/ directory).
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "VERSION",
        here.parent / "VERSION",                 # bundled: Contents/Resources/VERSION → ../
        Path(getattr(sys, "_MEIPASS", "")) / "VERSION" if getattr(sys, "_MEIPASS", "") else None,
    ]
    for p in candidates:
        if p and p.exists():
            return p.read_text().strip() or "0.0.0"
    return "0.0.0"


__version__ = _read_version()

GRANOLA_DIR = Path.home() / "Library/Application Support/Granola"
SUPABASE_FILE = GRANOLA_DIR / "supabase.json"
# Granola relocated the auth token here (newer app versions). supabase.json
# is legacy / often empty now; stored-accounts.json is the source of truth.
STORED_ACCOUNTS_FILE = GRANOLA_DIR / "stored-accounts.json"
CACHE_FILE = GRANOLA_DIR / "cache-v6.json"

API_BASE = "https://api.granola.ai/v1"
CLIENT_VERSION = "5.354.0"

# Default output folder.
# - When bundled (PyInstaller .app from a DMG, /Applications, etc.) the script
#   path is read-only, so use ~/Documents/Granola Export/.
# - When running from source, write next to the source files.
def _default_out_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path.home() / "Documents" / "Granola Export"
    here = Path(__file__).resolve().parent
    # Treat any path that's clearly inside a .app bundle or a /Volumes mount as
    # read-only and fall back to ~/Documents.
    parts = here.parts
    if any(p.endswith(".app") for p in parts) or (parts and parts[0] == "/" and len(parts) > 1 and parts[1] == "Volumes"):
        return Path.home() / "Documents" / "Granola Export"
    return here


DEFAULT_OUT_ROOT = _default_out_root()

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


AUTH_CACHE_FILE = PREFS_DIR / "auth-cache.json"


def _decode_jwt_payload(jwt: str) -> dict:
    """Decode a JWT's payload section. No signature verification."""
    import base64
    parts = jwt.split(".")
    if len(parts) < 2:
        return {}
    p = parts[1]
    p += "=" * (-len(p) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def _bundle_remaining_seconds(bundle: dict) -> int:
    """Seconds until access_token expires. Prefers JWT exp claim; falls back
    to obtained_at + expires_in. Returns 0 (expired) if unknown."""
    token = bundle.get("access_token", "")
    if token:
        payload = _decode_jwt_payload(token)
        exp = payload.get("exp")
        if exp:
            return max(0, int(exp - time.time()))
    obtained_at = bundle.get("obtained_at", 0)
    expires_in = bundle.get("expires_in", 0)
    if obtained_at and expires_in:
        obtained_s = obtained_at / 1000 if obtained_at > 10**12 else obtained_at
        return max(0, int(obtained_s + expires_in - time.time()))
    return 0


def _load_auth_cache() -> Optional[dict]:
    """Read our private cache of refreshed tokens (separate from supabase.json
    so we don't fight Granola's writes)."""
    if not AUTH_CACHE_FILE.exists():
        return None
    try:
        return json.loads(AUTH_CACHE_FILE.read_text())
    except Exception:
        return None


def _save_auth_cache(bundle: dict) -> None:
    try:
        AUTH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTH_CACHE_FILE.write_text(json.dumps(bundle, indent=2))
    except Exception:
        pass


# Module-level capture of the most recent refresh failure (for diagnostics).
_LAST_REFRESH_ERROR: str = ""


def _refresh_workos_token(bundle: dict, log: Optional[Callable[[str], None]] = None) -> Optional[dict]:
    """Hit WorkOS's /user_management/authenticate to swap a refresh_token for
    a new access_token + refresh_token pair. Returns a fresh bundle on
    success, None on failure (sets _LAST_REFRESH_ERROR with details).

    If `log` is provided, every step is reported to it as a string."""
    global _LAST_REFRESH_ERROR
    _LAST_REFRESH_ERROR = ""

    def _log(msg: str):
        if log:
            log(msg)

    refresh_token = bundle.get("refresh_token")
    access_token = bundle.get("access_token")
    if not refresh_token or not access_token:
        _LAST_REFRESH_ERROR = "no refresh_token or access_token in bundle"
        _log(f"   ✗ {_LAST_REFRESH_ERROR}")
        return None
    _log(f"   refresh_token (suffix): …{refresh_token[-6:]}")

    payload = _decode_jwt_payload(access_token)
    iss = (payload.get("iss") or "").rstrip("/")
    if not iss:
        _LAST_REFRESH_ERROR = "could not decode JWT iss claim"
        _log(f"   ✗ {_LAST_REFRESH_ERROR}")
        return None
    parts = iss.split("/")
    client_id = parts[-1] if parts and parts[-1].startswith("client_") else None
    auth_host = "/".join(parts[:3]) if len(parts) >= 3 else None
    if not client_id or not auth_host:
        _LAST_REFRESH_ERROR = f"could not parse auth_host/client_id from iss={iss!r}"
        _log(f"   ✗ {_LAST_REFRESH_ERROR}")
        return None
    _log(f"   auth_host: {auth_host}")
    _log(f"   client_id: {client_id}")

    body = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    url = f"{auth_host}/user_management/authenticate"
    _log(f"   POST {url}")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"Granola/{CLIENT_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                import zlib
                raw = zlib.decompress(raw)
            result = json.loads(raw.decode("utf-8"))
            _log(f"   ✓ HTTP 200 — keys: {list(result.keys())}")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
            try:
                body_json = json.loads(body_text)
                err_code = body_json.get("error", "")
                err_desc = body_json.get("error_description", body_text[:200])
            except Exception:
                err_code, err_desc = "", body_text[:200]
        except Exception:
            err_code, err_desc = "", str(e)
        _LAST_REFRESH_ERROR = f"HTTP {e.code} {err_code}: {err_desc}"
        _log(f"   ✗ {_LAST_REFRESH_ERROR}")
        return None
    except Exception as e:
        _LAST_REFRESH_ERROR = f"{type(e).__name__}: {e}"
        _log(f"   ✗ {_LAST_REFRESH_ERROR}")
        return None

    new_access = result.get("access_token")
    if not new_access:
        _LAST_REFRESH_ERROR = f"response missing access_token: keys={list(result.keys())}"
        _log(f"   ✗ {_LAST_REFRESH_ERROR}")
        return None
    new_remaining = max(60, int(_decode_jwt_payload(new_access).get("exp", 0) - time.time())) or 21599
    _log(f"   new access_token good for {new_remaining // 60}min")
    return {
        "access_token": new_access,
        "refresh_token": result.get("refresh_token", refresh_token),
        "obtained_at": int(time.time() * 1000),
        "expires_in": new_remaining,
        "token_type": "Bearer",
    }


def get_last_refresh_error() -> str:
    return _LAST_REFRESH_ERROR


def load_access_token(log: Optional[Callable[[str], None]] = None) -> tuple[str, str, int]:
    """Returns (token, source_name, seconds_remaining). Raises AuthError if
    no valid token can be obtained — even after attempting a refresh.

    If `log` is provided, every step is reported as a string — useful for
    the 'Diagnose connection' button in Settings."""
    def _log(msg: str):
        if log:
            log(msg)

    _log("[Step 1] Checking ~/Library/Application Support/Granola/supabase.json")
    if not SUPABASE_FILE.exists():
        _log("   ✗ file does not exist — is Granola installed?")
        raise AuthError(f"{SUPABASE_FILE} not found — is Granola installed?")

    try:
        st = SUPABASE_FILE.stat()
        from datetime import datetime as _dt
        mtime_str = _dt.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        _log(f"   ✓ found ({st.st_size} bytes, last written {mtime_str})")
    except Exception as e:
        _log(f"   ! couldn't stat: {e}")

    # Pass 1: prefer a still-fresh access_token from supabase.json (Granola
    # is the source of truth).
    sb_bundle: Optional[dict] = None
    sb_name: Optional[str] = None
    _log("[Step 2] Parsing supabase.json")
    try:
        with open(SUPABASE_FILE) as f:
            outer = json.load(f)
        _log(f"   ✓ top-level keys: {list(outer.keys())}")
    except Exception as e:
        _log(f"   ✗ {type(e).__name__}: {e}")
        outer = {}

    for name in ("workos_tokens", "cognito_tokens"):
        _log(f"[Step 3] Checking {name}")
        if name not in outer:
            _log(f"   ✗ key not present in supabase.json")
            continue
        try:
            bundle = json.loads(outer[name])
        except Exception as e:
            _log(f"   ✗ couldn't parse inner JSON: {e}")
            continue
        if not bundle.get("access_token"):
            _log(f"   ✗ no access_token in bundle")
            continue
        remaining = _bundle_remaining_seconds(bundle)
        rt = bundle.get("refresh_token", "")
        _log(f"   access_token (length {len(bundle['access_token'])}), "
             f"refresh_token (suffix …{rt[-6:] if rt else 'none'})")
        if remaining > 60:
            _log(f"   ✓ FRESH — expires in {remaining // 60}min, USING IT")
            return bundle["access_token"], name, remaining
        if remaining <= 0:
            _log(f"   ✗ expired (no time remaining)")
        else:
            _log(f"   ✗ near-expiry ({remaining}s left, threshold 60s)")
        # Remember the first non-empty bundle for refresh attempts below
        if sb_bundle is None:
            sb_bundle, sb_name = bundle, name

    # Pass 2: still-fresh access_token from our private auth-cache (left over
    # from a recent refresh).
    _log(f"[Step 4] Checking our private cache: {AUTH_CACHE_FILE}")
    cached = _load_auth_cache()
    if cached and cached.get("access_token"):
        remaining = _bundle_remaining_seconds(cached)
        crt = cached.get("refresh_token", "")
        _log(f"   ✓ cache present (refresh_token suffix …{crt[-6:] if crt else 'none'})")
        if remaining > 60:
            _log(f"   ✓ cached access_token still FRESH — expires in {remaining // 60}min, USING IT")
            return cached["access_token"], "auth-cache", remaining
        else:
            _log(f"   ✗ cached access_token expired ({remaining}s left)")
    else:
        _log("   ✗ cache absent or empty")

    # Pass 3: try refreshing. Prefer the cached refresh_token (newer due to
    # rotation) then fall back to the supabase.json one.
    _log("[Step 5] Attempting WorkOS refresh")
    refresh_candidates: list[tuple[str, dict]] = []
    if cached and cached.get("refresh_token") and cached.get("access_token"):
        refresh_candidates.append(("our auth-cache", cached))
    if sb_bundle and sb_bundle.get("refresh_token"):
        if not refresh_candidates or refresh_candidates[0][1].get("refresh_token") != sb_bundle.get("refresh_token"):
            refresh_candidates.append((f"supabase.json {sb_name}", sb_bundle))

    if not refresh_candidates:
        _log("   ✗ no refresh_token available to try")

    last_err = ""
    for source_label, source_bundle in refresh_candidates:
        _log(f"   Trying refresh from: {source_label}")
        new_bundle = _refresh_workos_token(source_bundle, log=log)
        if new_bundle:
            _save_auth_cache(new_bundle)
            _log(f"   ✓ Saved new bundle to {AUTH_CACHE_FILE}")
            remaining = _bundle_remaining_seconds(new_bundle)
            return new_bundle["access_token"], "workos_tokens (refreshed)", remaining
        last_err = get_last_refresh_error()

    # WorkOS explicitly told us the session is dead — a different scenario from
    # a merely-rotated refresh_token. Often tied to billing / forced sign-out
    # on Granola's side, in which case nothing the user does in the local app
    # will help until they sign back into Granola.
    low_err = last_err.lower()
    if "session has already ended" in low_err or "session_not_found" in low_err:
        raise AuthError(
            "Granola has ended your session at the server (HTTP 400: 'Session has already ended').\n\n"
            "This is most often caused by:\n"
            "  • A failed Granola subscription payment (account auto-downgraded)\n"
            "  • You signed out from Granola somewhere else (web, another Mac)\n"
            "  • Granola admin revoked the session\n\n"
            "To fix:\n\n"
            "  1. If you have a payment failure showing in Granola → update your card or downgrade to the free plan\n"
            "  2. Quit the Granola desktop app completely (Cmd+Q)\n"
            "  3. Re-open Granola and sign in fresh with Google\n"
            "  4. Once you see your meetings load, click Reconnect here\n\n"
            "Your already-exported transcripts on disk are unaffected by any of this."
        )
    if "already exchanged" in low_err or "invalid_grant" in low_err:
        raise AuthError(
            "Your saved Granola refresh token is no longer valid — it's been used. "
            "Granola itself hasn't issued a new one yet, which usually means Granola's UI is showing cached data.\n\n"
            "To force Granola to re-authorise:\n\n"
            "  1. Quit Granola completely  (Cmd+Q while Granola is focused, or right-click its Dock icon → Quit)\n"
            "  2. Re-open Granola from the Applications folder\n"
            "  3. If prompted, sign in with your Google account\n"
            "  4. Once you see your meetings, click Reconnect here\n\n"
            "(After this one-time fix the app auto-refreshes forever.)"
        )
    if last_err:
        raise AuthError(f"Granola session expired and refresh failed: {last_err}")
    raise AuthError("Granola session expired. Open the Granola app and sign in, then click Reconnect.")


def diagnose_connection(log: Callable[[str], None]) -> None:
    """Run the full connection flow with verbose logging. Catches AuthError
    so the diagnose pass always reports a final outcome instead of raising."""
    log("───────────────────────────────────────────────────────")
    log("Diagnose: walking through the full connection flow")
    log("───────────────────────────────────────────────────────")
    try:
        token, source, remaining = load_access_token(log=log)
        log("───────────────────────────────────────────────────────")
        log(f"✓ FINAL RESULT: connected via {source}")
        log(f"  access_token good for {remaining // 60}min")
        log("───────────────────────────────────────────────────────")
    except AuthError as e:
        log("───────────────────────────────────────────────────────")
        log(f"✗ FINAL RESULT: failed")
        for ln in str(e).split("\n"):
            log(f"  {ln}")
        log("───────────────────────────────────────────────────────")


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


# ---------- one-shot manual Granola sync (opt-in, never auto) -------------

def load_stored_account_token() -> tuple[str, int]:
    """Read the access token from Granola's current location.

    Newer Granola versions store it in stored-accounts.json (the legacy
    supabase.json is often empty now). Returns (token, seconds_remaining).
    Raises AuthError if missing or expired — caller surfaces this as a
    plain status message, NOT a modal.
    """
    if not STORED_ACCOUNTS_FILE.exists():
        raise AuthError(
            "stored-accounts.json not found. Open the Granola app and sign in, "
            "then try Sync again."
        )
    try:
        outer = json.loads(STORED_ACCOUNTS_FILE.read_text())
        accounts = json.loads(outer["accounts"])
    except Exception as e:
        raise AuthError(f"couldn't parse stored-accounts.json: {e}")
    if not accounts:
        raise AuthError("no Granola accounts found — sign in to Granola first")
    try:
        tokens = json.loads(accounts[0]["tokens"])
        token = tokens["access_token"]
    except Exception as e:
        raise AuthError(f"no access_token in stored account: {e}")
    payload = _decode_jwt_payload(token)
    remaining = int(payload.get("exp", 0) - time.time())
    if remaining <= 0:
        raise AuthError(
            "Granola token expired. Open the Granola app (it refreshes the "
            "token automatically), then try Sync again."
        )
    return token, remaining


# Official Granola public API (https://docs.granola.ai/introduction).
# Stable, key-authed — unlike the reverse-engineered WorkOS token path.
GRANOLA_API_BASE = "https://public-api.granola.ai/v1"


def _granola_api_get(path: str, api_key: str, timeout: int = 30):
    req = urllib.request.Request(
        f"{GRANOLA_API_BASE}{path}",
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "GranolaExport/1.x",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding", "").lower()
        if enc == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            import zlib
            raw = zlib.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def granola_api_list_notes(api_key: str, cursor: Optional[str] = None) -> tuple[list[dict], bool, Optional[str]]:
    """One page of GET /notes. Returns (notes, has_more, next_cursor)."""
    path = "/notes"
    if cursor:
        path += f"?cursor={urllib.parse.quote(cursor)}"
    data = _granola_api_get(path, api_key)
    return data.get("notes", []), bool(data.get("hasMore")), data.get("cursor")


def granola_api_get_note(api_key: str, note_id: str) -> dict:
    """GET /notes/{id}?include=transcript — full note + transcript array."""
    return _granola_api_get(
        f"/notes/{urllib.parse.quote(note_id)}?include=transcript", api_key
    )


def _api_note_to_doc(note: dict) -> dict:
    """Map an official-API note into the doc dict write_meeting_file expects."""
    attendees = note.get("attendees") or []
    owner = note.get("owner") or {}
    people_list = list(attendees)
    if owner and owner not in people_list:
        people_list = [owner] + people_list
    return {
        "id": note.get("id") or "",
        "title": note.get("title") or "Untitled",
        "created_at": note.get("created_at", ""),
        "updated_at": note.get("updated_at", ""),
        # extract_people() iterates dict-values that are lists of {name,email}
        "people": {"attendees": people_list},
        "notes_markdown": note.get("summary_markdown") or "",
        "summary": None,
    }


def _api_transcript_to_segments(transcript: list) -> list[dict]:
    """Map API transcript segments → the {source,text,start,end} shape that
    coalesce_transcript() consumes. speaker.source 'speaker' = the other
    party (→ Them via SPEAKER_LABELS['system']); 'microphone' = the user."""
    segs: list[dict] = []
    for s in transcript or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        src_raw = ((s.get("speaker") or {}).get("source") or "").lower()
        source = "microphone" if src_raw == "microphone" else "system"
        segs.append({
            "source": source,
            "text": text,
            "start": s.get("start_time", ""),
            "end": s.get("end_time", ""),
        })
    return segs


def _dedup_key(date_iso: str, title: str) -> str:
    """Stable `<local-date>_<slug>` key derived ONLY from the meeting's
    timestamp + title — never the id. Symmetric across the old
    reverse-engineered export (8-hex ids) and the official API (not_ ids),
    which otherwise produce different filenames for the same meeting."""
    dt = parse_iso(date_iso)
    ds = dt.astimezone().strftime("%Y-%m-%d_%H%M") if dt else "0000-00-00_0000"
    return f"{ds}_{slugify(title or 'Untitled')}"


def _local_dedup_keys(transcripts_dir: Path) -> set[str]:
    """One (date, slug) key per existing transcript, parsed from frontmatter
    so re-syncing never duplicates the ~400 already-exported meetings."""
    keys: set[str] = set()
    for m in collect_existing_meta(transcripts_dir):
        keys.add(_dedup_key(m.date, m.title))
    return keys


def _doc_dedup_key(doc: dict) -> str:
    return _dedup_key(doc.get("created_at", ""), doc.get("title") or "Untitled")


def granola_sync(out_root: Path,
                  api_key: str,
                  log: Optional[Callable[[str], None]] = None,
                  on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """Pull every Granola note not already present locally via the OFFICIAL
    public API and write it as Markdown. Manual, one-shot, key-authed.

    Reuses write_meeting_file / coalesce_transcript / write_index unchanged.
    """
    def _log(m: str):
        if log:
            log(m)

    if not api_key:
        raise AuthError(
            "No Granola API key set. Add one in Settings → Granola API key "
            "(create a key at https://docs.granola.ai/introduction)."
        )

    out_dir = out_root / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    existing_keys = _local_dedup_keys(out_dir)

    # 1) Page through the full note list (cursor-based).
    notes: list[dict] = []
    cursor = None
    try:
        while True:
            page, has_more, cursor = granola_api_list_notes(api_key, cursor)
            notes.extend(page)
            _log(f"[sync] listed {len(notes)} notes…")
            if not has_more or not cursor:
                break
            time.sleep(0.25)  # well under the 5 req/s sustained limit
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError("Granola API key rejected (401/403). Check the key in Settings.")
        raise

    # 2) Keep only notes whose date+slug isn't already on disk.
    candidates = []
    for n in notes:
        doc_stub = {"id": n.get("id", ""), "title": n.get("title") or "Untitled",
                    "created_at": n.get("created_at", "")}
        if n.get("id") and _doc_dedup_key(doc_stub) not in existing_keys:
            candidates.append(n)
    _log(f"[sync] {len(notes)} notes in Granola, {len(candidates)} new to import")

    entries_by_name = {m.filename: m for m in collect_existing_meta(out_dir)}
    fetched = errors = no_tx = 0
    for i, note in enumerate(candidates, 1):
        title = note.get("title") or "Untitled"
        try:
            full = granola_api_get_note(api_key, note["id"])
            doc = _api_note_to_doc(full)
            segments = _api_transcript_to_segments(full.get("transcript") or [])
            if segments:
                fetched += 1
            else:
                no_tx += 1
            _, meta = write_meeting_file(out_dir, doc, segments or None)
            entries_by_name[meta.filename] = meta
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _log("[sync] rate-limited (429) — backing off 5s")
                time.sleep(5)
                continue
            if e.code in (401, 403):
                _log("[sync] API key rejected mid-sync — stopping")
                break
            errors += 1
            _log(f"[sync] HTTP {e.code} for {title}")
        except Exception as e:
            errors += 1
            _log(f"[sync] error on {title}: {e}")

        if on_progress:
            try:
                on_progress(i, len(candidates))
            except Exception:
                pass
        time.sleep(0.22)  # ~4.5 req/s, under the 5 req/s sustained ceiling

    try:
        write_index(out_root, list(entries_by_name.values()))
    except Exception as e:
        _log(f"[sync] INDEX.md error: {e}")

    stats = {
        "total": len(notes),
        "new": len(candidates),
        "with_transcript": fetched,
        "no_transcript": no_tx,
        "errors": errors,
    }
    _log(f"[sync] done — imported {len(candidates)} "
         f"({fetched} with transcripts, {no_tx} notes-only, {errors} errors)")
    return stats


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


def _parse_frontmatter(text: str) -> dict:
    """Parse the leading YAML-ish frontmatter block of a transcript .md."""
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"')
    return fm


def collect_existing_meta(transcripts_dir: Path) -> list[MeetingMeta]:
    """Best-effort parse of existing files for index regeneration."""
    out: list[MeetingMeta] = []
    if not transcripts_dir.exists():
        return out
    for p in transcripts_dir.glob("*.md"):
        try:
            fm = _parse_frontmatter(p.read_text())
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


def load_local_docs(transcripts_dir: Path) -> list[dict]:
    """Build Granola-doc-shaped dicts from the local transcripts folder.

    This is the Granola-free replacement for load_documents(): no auth, no
    network, no cache — just the user's own exported/recorded .md files.
    The returned dicts carry the keys the GUI's row renderer + detail
    window already expect, plus `_local_path` so the detail view can read
    the file directly instead of reconstructing a path.
    """
    docs: list[dict] = []
    if not transcripts_dir.exists():
        return docs
    for p in sorted(transcripts_dir.glob("*.md")):
        try:
            fm = _parse_frontmatter(p.read_text())
        except Exception:
            fm = {}
        docs.append({
            "id": fm.get("id") or p.stem,
            "title": fm.get("title") or p.stem,
            "created_at": fm.get("date", ""),
            "updated_at": fm.get("updated_at", ""),
            "people": {},
            "notes_markdown": "",
            "has_transcript": fm.get("has_transcript", "false") == "true",
            "_local_path": str(p),
            "_filename": p.name,
        })
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return docs
