"""Google Calendar read-only adapter. List upcoming events only."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from jarvis.audit import AuditLog
from jarvis.integrations.limits import RateLimiter
from jarvis.integrations.oauth_google import CALENDAR_SCOPE
from jarvis.safety.halt import HaltController, HaltedError

_TIMEOUT_S = 20.0
_MAX_EVENTS = 20
_MAX_DESCRIPTION = 1000
_MAX_DAYS = 14
_ID = re.compile(r"^[0-9a-f]{32}$")
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarIntegration:
    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        oauth,
        store,
        transport=None,
        limiter: RateLimiter | None = None,
        clock=None,
        ttl_s: float = 120.0,
    ) -> None:
        self._halt = halt
        self._audit = audit
        self._oauth = oauth
        self._store = store
        self._transport = transport
        self._limiter = limiter or RateLimiter(max_calls=10, window_s=60.0, clock=clock)
        self._clock = clock or _monotonic
        self._ttl_s = ttl_s
        self._enabled = False
        self._lock = threading.RLock()
        self._pending: dict | None = None
        self._used: set[str] = set()
        self._last: dict | None = None
        self._notice = ""

    def status(self) -> dict:
        with self._lock:
            self._expire_locked()
            return self._view_locked()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._cancel_pending_locked()
                self._last = None
                self._notice = "Calendar read is off."
            else:
                self._notice = "Calendar read is on. Load events only when you ask."
            return self._enabled

    def request_upcoming(self, days: object = 7) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._fail("The action was cancelled.")
            if not self._enabled:
                self._audit.write(status="calendar_rejected", outcome="rejected", detail="Calendar read is not enabled")
                return self._fail("Calendar read is not enabled.")
            try:
                span = int(days)
            except (TypeError, ValueError):
                return self._fail("That calendar range is not valid.")
            if span < 1 or span > _MAX_DAYS:
                return self._fail("That calendar range is not allowed.")
            return self._queue_locked({"days": span}, f"Read upcoming calendar events for {span} day(s).")

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked()
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="calendar_rejected", outcome="rejected", detail="Calendar action rejected")
                return self._fail("That action is not waiting for approval.")
            pending = self._pending
            if pending is None or pending["action_id"] != action_id:
                return self._fail("That action is not waiting for approval.")
            if not self._halt.is_current(pending["generation"]) or self._halt.approval_state(action_id) != "pending":
                self._consume_locked(action_id)
                return self._fail("The action was cancelled.")
            if type(approved) is not bool:
                return self._fail("That action is not waiting for approval.")
            if not approved:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                self._audit.write(status="calendar_denied", outcome="denied", detail="Calendar action denied")
                return self._fail("The action was denied.")
            if not self._enabled:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                return self._fail("Calendar read is not enabled.")
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                return self._fail("The action was cancelled.")
            days = int(pending["args"]["days"])
            generation = pending["generation"]
            self._consume_locked(action_id)

        if not self._limiter.allow():
            with self._lock:
                return self._fail("Calendar rate limit reached. Try again later.")
        tokens = self._store.load()
        if tokens is None:
            with self._lock:
                return self._fail("Connect Google before reading Calendar.")
        if CALENDAR_SCOPE not in tokens.get("scopes", []):
            with self._lock:
                return self._fail("Google did not grant Calendar read access.")
        refreshed = self._oauth.refresh_if_needed(tokens)
        if refreshed is None:
            with self._lock:
                return self._fail("Google sign-in expired. Connect again.")
        if not self._halt.is_current(generation):
            with self._lock:
                return self._fail("The action was cancelled.")
        try:
            events = self._list_events(refreshed["access_token"], days, generation)
            with self._lock:
                self._last = {"kind": "upcoming", "days": days, "events": events}
                self._notice = f"Loaded {len(events)} calendar event(s)."
                self._audit.write(status="calendar_events", outcome="completed", detail="Calendar events read")
                return self._view_locked()
        except _CalendarError as exc:
            with self._lock:
                self._audit.write(status="calendar_failed", outcome="failed", detail="Calendar request failed")
                return self._fail(exc.text)
        except Exception:
            with self._lock:
                self._audit.write(status="calendar_failed", outcome="failed", detail="Calendar request failed")
                return self._fail("Calendar request failed.")

    def clear_result(self) -> dict:
        with self._lock:
            self._last = None
            self._notice = "Calendar results cleared."
            return self._view_locked()

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._cancel_pending_locked()
                self._last = None
                self._notice = ""

    def _queue_locked(self, args: dict, summary: str) -> dict:
        if self._pending is not None:
            return self._fail("A calendar action is already waiting.")
        generation = self._halt.capture()
        if not self._halt.is_current(generation):
            return self._fail("The action was cancelled.")
        action_id = uuid.uuid4().hex
        if action_id in self._used or self._halt.approval_state(action_id) is not None:
            return self._fail("That action is not allowed.")
        try:
            self._halt.request_approval(action_id)
        except HaltedError:
            self._used.add(action_id)
            return self._fail("The action was cancelled.")
        self._pending = {
            "action_id": action_id,
            "args": args,
            "summary": summary,
            "generation": generation,
            "expires": self._clock() + self._ttl_s,
        }
        self._notice = summary
        self._audit.write(status="calendar_requested", outcome="pending", detail="Calendar action requested")
        return self._view_locked()

    def _list_events(self, access_token: str, days: int, generation: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)
        result = self._halt.guarded_get(
            _EVENTS_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(_MAX_EVENTS),
                "timeMin": now.isoformat().replace("+00:00", "Z"),
                "timeMax": end.isoformat().replace("+00:00", "Z"),
            },
            timeout=_TIMEOUT_S,
            transport=self._transport,
        )
        if not self._halt.is_current(generation) or result.outcome in {"cancelled", "already_completed"}:
            raise _CalendarError("The action was cancelled.")
        if result.outcome != "ok" or result.response is None or result.response.status_code != 200:
            raise _CalendarError("Calendar request failed.")
        try:
            payload = result.response.json()
        except ValueError as exc:
            raise _CalendarError("Calendar returned invalid data.") from exc
        if not isinstance(payload, dict):
            raise _CalendarError("Calendar returned invalid data.")
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise _CalendarError("Calendar returned invalid data.")
        events = []
        for item in items[:_MAX_EVENTS]:
            if not isinstance(item, dict):
                continue
            row = _public_event(item)
            if row is not None:
                events.append(row)
        return events

    def _expire_locked(self) -> str | None:
        pending = self._pending
        if pending is None:
            return None
        if not self._halt.is_current(pending["generation"]) or self._clock() >= pending["expires"]:
            action_id = pending["action_id"]
            if self._halt.approval_state(action_id) == "pending":
                self._halt.resolve_approval(action_id, "denied")
            self._consume_locked(action_id)
            self._notice = "That approval has expired." if self._halt.is_current(pending["generation"]) else "The action was cancelled."
            return action_id
        return None

    def _cancel_pending_locked(self) -> None:
        pending = self._pending
        if pending is None:
            return
        action_id = pending["action_id"]
        if self._halt.approval_state(action_id) == "pending":
            self._halt.resolve_approval(action_id, "denied")
        self._consume_locked(action_id)

    def _consume_locked(self, action_id: str) -> None:
        self._used.add(action_id)
        if self._pending is not None and self._pending["action_id"] == action_id:
            self._pending = None

    def _fail(self, text: str) -> dict:
        self._notice = text
        return self._view_locked()

    def _view_locked(self) -> dict:
        pending = None
        if self._pending is not None:
            pending = {
                "action_id": self._pending["action_id"],
                "summary": self._pending["summary"],
                "state": "waiting for approval",
            }
        return {
            "ok": self._pending is not None or self._last is not None,
            "permission_enabled": self._enabled,
            "text": self._notice,
            "pending": pending,
            "result": self._last,
        }


def _public_event(item: dict) -> dict | None:
    event_id = item.get("id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 1024:
        return None
    title = item.get("summary", "")
    location = item.get("location", "")
    description = item.get("description", "")
    if not isinstance(title, str):
        title = ""
    if not isinstance(location, str):
        location = ""
    if not isinstance(description, str):
        description = ""
    start = _event_time(item.get("start"))
    end = _event_time(item.get("end"))
    if start is None or end is None:
        return None
    if len(description) > _MAX_DESCRIPTION:
        description = description[:_MAX_DESCRIPTION] + "\n[truncated]"
    return {
        "id": event_id[:128],
        "title": title[:200],
        "start": start,
        "end": end,
        "location": location[:200],
        "description": description,
    }


def _event_time(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    stamp = value.get("dateTime") or value.get("date")
    if not isinstance(stamp, str) or not stamp or len(stamp) > 64:
        return None
    return stamp


def _monotonic() -> float:
    import time

    return time.monotonic()


class _CalendarError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
