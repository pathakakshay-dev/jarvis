"""Integrations facade: registry, Google OAuth lifecycle, Gmail and Calendar."""

from __future__ import annotations

from pathlib import Path

from jarvis.audit import AuditLog
from jarvis.integrations.calendar_google import CalendarIntegration
from jarvis.integrations.gmail import GmailIntegration
from jarvis.integrations.oauth_google import GoogleOAuth
from jarvis.integrations.protect import TokenProtector
from jarvis.integrations.store import TokenStore
from jarvis.safety.halt import HaltController

_REGISTRY = (
    {
        "id": "google_gmail",
        "name": "Gmail",
        "provider": "google",
        "access": "read-only",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    },
    {
        "id": "google_calendar",
        "name": "Google Calendar",
        "provider": "google",
        "access": "read-only",
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
    },
)


class IntegrationsService:
    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        data_dir: Path,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
        protector: TokenProtector | None = None,
        transport=None,
        clock=None,
    ) -> None:
        self._halt = halt
        self._audit = audit
        self._store = TokenStore(Path(data_dir) / "integrations" / "google.tokens.dpapi", protector=protector)
        self._oauth = GoogleOAuth(
            halt,
            audit,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            store=self._store,
            transport=transport,
            clock=clock,
        )
        self.gmail = GmailIntegration(halt, audit, oauth=self._oauth, store=self._store, transport=transport, clock=clock)
        self.calendar = CalendarIntegration(halt, audit, oauth=self._oauth, store=self._store, transport=transport, clock=clock)

    def registry(self) -> list[dict]:
        connected = self._store.load() is not None
        return [
            {
                **item,
                "connected": connected,
                "permission_enabled": self.gmail.status()["permission_enabled"]
                if item["id"] == "google_gmail"
                else self.calendar.status()["permission_enabled"],
            }
            for item in _REGISTRY
        ]

    def status(self) -> dict:
        tokens = self._store.load()
        scopes = list((tokens or {}).get("scopes") or [])
        return {
            "configured": self._oauth.configured(),
            "connected": tokens is not None,
            "scopes": scopes,
            "registry": self.registry(),
            "gmail": self.gmail.status(),
            "calendar": self.calendar.status(),
            "text": self._status_text(tokens is not None),
        }

    def begin_connect(self) -> dict:
        return self._oauth.begin()

    def complete_connect(self, *, state: object, code: object, error: object = "") -> dict:
        return self._oauth.complete(state=state, code=code, error=error)

    def disconnect(self) -> dict:
        self.gmail.clear_result()
        self.calendar.clear_result()
        self.gmail.set_enabled(False)
        self.calendar.set_enabled(False)
        return self._oauth.revoke_and_clear()

    def observe_stop(self) -> None:
        self._oauth.observe_stop()
        self.gmail.observe_stop()
        self.calendar.observe_stop()

    def _status_text(self, connected: bool) -> str:
        if not self._oauth.configured():
            return "Google OAuth client settings are missing."
        if connected:
            return "Google account connected. Enable Gmail or Calendar read, then request data explicitly."
        return "Google account is not connected."
