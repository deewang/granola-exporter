"""Tiny macOS Keychain wrapper via the `security` CLI.

Used to store the Granola API key (and later the Anthropic key) without
ever writing them to disk in plaintext or committing them to the repo.
"""

from __future__ import annotations

import subprocess

SERVICE_GRANOLA_API = "com.davidwang.granolaexport.granola-api"
_ACCOUNT = "default"


def set_secret(service: str, value: str) -> bool:
    """Store (or overwrite) a secret. Returns True on success."""
    if not value:
        return delete_secret(service)
    try:
        # -U updates if it already exists; -w takes the secret value.
        subprocess.run(
            ["security", "add-generic-password",
             "-a", _ACCOUNT, "-s", service, "-w", value, "-U"],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


def get_secret(service: str) -> str:
    """Return the stored secret, or '' if not set / unreadable."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-a", _ACCOUNT, "-s", service, "-w"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def delete_secret(service: str) -> bool:
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-a", _ACCOUNT, "-s", service],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


# Convenience wrappers for the Granola API key.
def set_granola_api_key(key: str) -> bool:
    return set_secret(SERVICE_GRANOLA_API, key.strip())


def get_granola_api_key() -> str:
    return get_secret(SERVICE_GRANOLA_API)
