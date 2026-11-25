"""Encrypted Google token file. Never writes plaintext tokens to disk."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from jarvis.integrations.protect import ProtectorError, TokenProtector, default_protector

_ALLOWED_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
)


class TokenStore:
    def __init__(self, path: Path, protector: TokenProtector | None = None) -> None:
        self._path = path
        self._protector = protector or default_protector()
        self._lock = threading.RLock()

    def load(self) -> dict | None:
        with self._lock:
            if not self._path.is_file():
                return None
            try:
                blob = self._path.read_bytes()
                raw = self._protector.unprotect(blob)
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError, UnicodeError, ProtectorError, json.JSONDecodeError):
                return None
            return _validated(payload)

    def save(self, payload: dict) -> None:
        cleaned = _validated(payload)
        if cleaned is None:
            raise ProtectorError("invalid token payload")
        raw = json.dumps(cleaned, separators=(",", ":"), sort_keys=True).encode("utf-8")
        blob = self._protector.protect(raw)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    def clear(self) -> None:
        with self._lock:
            for candidate in (self._path, self._path.with_suffix(self._path.suffix + ".tmp")):
                try:
                    if candidate.is_file():
                        candidate.unlink()
                except OSError:
                    pass


def _validated(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    access = payload.get("access_token")
    refresh = payload.get("refresh_token", "")
    token_type = payload.get("token_type", "Bearer")
    expires_at = payload.get("expires_at", 0)
    scopes = payload.get("scopes", [])
    if not isinstance(access, str) or not access or len(access) > 8192:
        return None
    if not isinstance(refresh, str) or len(refresh) > 8192:
        return None
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        return None
    try:
        expires_at = float(expires_at)
    except (TypeError, ValueError):
        return None
    if not isinstance(scopes, list) or not scopes:
        return None
    cleaned_scopes = []
    for item in scopes:
        if not isinstance(item, str) or item not in _ALLOWED_SCOPES:
            return None
        if item not in cleaned_scopes:
            cleaned_scopes.append(item)
    required = {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    }
    if not required.issubset(set(cleaned_scopes)):
        # Allow storing whichever readonly scopes were granted; callers check needed scope.
        pass
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "scopes": cleaned_scopes,
    }
