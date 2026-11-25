"""Google OAuth authorization-code + PKCE. No passwords, no browser automation."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
_SCOPES = (GMAIL_SCOPE, CALENDAR_SCOPE)
_TTL_S = 600.0
_TIMEOUT_S = 20.0


class GoogleOAuth:
    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        store,
        transport=None,
        clock=None,
    ) -> None:
        self._halt = halt
        self._audit = audit
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._redirect_uri = redirect_uri
        self._store = store
        self._transport = transport
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._pending: dict | None = None
        self._notice = ""

    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._redirect_uri.startswith("http://127.0.0.1:"))

    def begin(self) -> dict:
        with self._lock:
            if self._halt.halted:
                return {"ok": False, "text": "The action was cancelled.", "auth_url": ""}
            if not self.configured():
                self._audit.write(status="integration_oauth_rejected", outcome="rejected", detail="Google OAuth is not configured")
                return {
                    "ok": False,
                    "text": "Set JARVIS_GOOGLE_CLIENT_ID and JARVIS_GOOGLE_CLIENT_SECRET in .env first.",
                    "auth_url": "",
                }
            generation = self._halt.capture()
            if not self._halt.is_current(generation):
                return {"ok": False, "text": "The action was cancelled.", "auth_url": ""}
            verifier = _token_urlsafe(64)
            challenge = _pkce_challenge(verifier)
            state = _token_urlsafe(32)
            self._pending = {
                "state": state,
                "verifier": verifier,
                "generation": generation,
                "expires": self._clock() + _TTL_S,
            }
            params = {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": " ".join(_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "false",
            }
            auth_url = AUTH_URL + "?" + urlencode(params)
            self._notice = "Complete Google sign-in in your browser, then return here."
            self._audit.write(status="integration_oauth_started", outcome="pending", detail="Google OAuth started")
            return {"ok": True, "text": self._notice, "auth_url": auth_url, "state": state}

    def complete(self, *, state: object, code: object, error: object = "") -> dict:
        with self._lock:
            if self._halt.halted:
                self._pending = None
                return {"ok": False, "text": "The action was cancelled."}
            pending = self._pending
            if pending is None:
                self._audit.write(status="integration_oauth_rejected", outcome="rejected", detail="OAuth callback without pending state")
                return {"ok": False, "text": "That Google sign-in is not waiting."}
            if self._clock() >= pending["expires"] or not self._halt.is_current(pending["generation"]):
                self._pending = None
                return {"ok": False, "text": "That Google sign-in expired." if self._halt.is_current(pending["generation"]) else "The action was cancelled."}
            if not isinstance(state, str) or not state or state != pending["state"]:
                self._pending = None
                self._audit.write(status="integration_oauth_rejected", outcome="rejected", detail="OAuth state mismatch")
                return {"ok": False, "text": "That Google sign-in could not be verified."}
            if isinstance(error, str) and error.strip():
                self._pending = None
                self._audit.write(status="integration_oauth_failed", outcome="failed", detail="Google OAuth denied")
                return {"ok": False, "text": "Google sign-in was denied."}
            if not isinstance(code, str) or not code or len(code) > 2048:
                self._pending = None
                self._audit.write(status="integration_oauth_rejected", outcome="rejected", detail="OAuth code rejected")
                return {"ok": False, "text": "That Google sign-in could not be completed."}
            verifier = pending["verifier"]
            generation = pending["generation"]
            self._pending = None

        result = self._halt.guarded_form_post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_S,
            transport=self._transport,
        )
        if not self._halt.is_current(generation) or result.outcome in {"cancelled", "already_completed"}:
            return {"ok": False, "text": "The action was cancelled."}
        if result.outcome != "ok" or result.response is None or result.response.status_code != 200:
            self._audit.write(status="integration_oauth_failed", outcome="failed", detail="Google token exchange failed")
            return {"ok": False, "text": "Google sign-in failed."}
        try:
            payload = result.response.json()
        except ValueError:
            self._audit.write(status="integration_oauth_failed", outcome="failed", detail="Google token response malformed")
            return {"ok": False, "text": "Google sign-in failed."}
        tokens = _tokens_from_response(payload, self._wall_clock())
        if tokens is None:
            self._audit.write(status="integration_oauth_failed", outcome="failed", detail="Google token scopes rejected")
            return {"ok": False, "text": "Google did not grant the required read-only scopes."}
        try:
            self._store.save(tokens)
        except Exception:
            self._audit.write(status="integration_oauth_failed", outcome="failed", detail="Google token store failed")
            return {"ok": False, "text": "Google credentials could not be stored securely."}
        self._audit.write(status="integration_oauth_completed", outcome="completed", detail="Google account connected")
        return {"ok": True, "text": "Google account connected for read-only Gmail and Calendar."}

    def refresh_if_needed(self, tokens: dict, *, skew_s: float = 60.0) -> dict | None:
        expires_at = float(tokens.get("expires_at") or 0)
        if expires_at - skew_s > self._wall_clock() and tokens.get("access_token"):
            return tokens
        refresh = tokens.get("refresh_token") or ""
        if not isinstance(refresh, str) or not refresh:
            return None
        if self._halt.halted:
            return None
        generation = self._halt.capture()
        result = self._halt.guarded_form_post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_S,
            transport=self._transport,
        )
        if not self._halt.is_current(generation) or result.outcome != "ok" or result.response is None or result.response.status_code != 200:
            return None
        try:
            payload = result.response.json()
        except ValueError:
            return None
        if "refresh_token" not in payload:
            payload = dict(payload)
            payload["refresh_token"] = refresh
        if "scope" not in payload:
            payload = dict(payload)
            payload["scope"] = " ".join(tokens.get("scopes") or [])
        updated = _tokens_from_response(payload, self._wall_clock())
        if updated is None:
            return None
        try:
            self._store.save(updated)
        except Exception:
            return None
        return updated

    def revoke_and_clear(self) -> dict:
        tokens = self._store.load()
        self._store.clear()
        with self._lock:
            self._pending = None
        if tokens and tokens.get("refresh_token"):
            token = tokens.get("refresh_token") or tokens.get("access_token") or ""
            if token and not self._halt.halted:
                self._halt.guarded_form_post(
                    REVOKE_URL,
                    data={"token": token},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=_TIMEOUT_S,
                    transport=self._transport,
                )
        elif tokens and tokens.get("access_token") and not self._halt.halted:
            self._halt.guarded_form_post(
                REVOKE_URL,
                data={"token": tokens["access_token"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_TIMEOUT_S,
                transport=self._transport,
            )
        self._audit.write(status="integration_disconnected", outcome="completed", detail="Google account disconnected")
        return {"ok": True, "text": "Google account disconnected."}

    def cancel_pending(self) -> None:
        with self._lock:
            self._pending = None

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._pending = None

    def _wall_clock(self) -> float:
        return time.time()


def _tokens_from_response(payload: object, now: float) -> dict | None:
    if not isinstance(payload, dict):
        return None
    access = payload.get("access_token")
    refresh = payload.get("refresh_token", "")
    token_type = str(payload.get("token_type") or "Bearer")
    expires_in = payload.get("expires_in", 0)
    scope = payload.get("scope", "")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str):
        refresh = ""
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        return None
    if expires_in <= 0 or expires_in > 86400:
        return None
    if not isinstance(scope, str) or not scope.strip():
        return None
    scopes = [part for part in scope.split() if part]
    if GMAIL_SCOPE not in scopes or CALENDAR_SCOPE not in scopes:
        return None
    extra = [item for item in scopes if item not in {GMAIL_SCOPE, CALENDAR_SCOPE, "openid", "email", "profile", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"}]
    if extra:
        return None
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": now + float(expires_in),
        "scopes": scopes,
    }


def _token_urlsafe(size: int) -> str:
    return secrets.token_urlsafe(size)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
