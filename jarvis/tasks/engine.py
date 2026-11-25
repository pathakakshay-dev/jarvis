"""Task & Reminder Engine: CRUD, bounded recurrence, and notification-only ticks."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.audit import AuditLog
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret
from jarvis.tasks.notify import Notice, Notifier, QueueNotifier
from jarvis.tasks.store import ReminderStore

_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,'!?:;()\-]{0,119}$")
_NOTES = re.compile(r"^[A-Za-z0-9 .,'!?:;()\-\n]{0,400}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_STATUSES = frozenset({"pending", "due", "snoozed", "completed", "cancelled"})
_RECURRENCE = frozenset({"none", "daily", "weekly"})
_SNOOZE = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}
_ACTIVE = frozenset({"pending", "due", "snoozed"})
_MAX_RECURRENCE_DAYS = 365
_TICK_S = 2.0


class TaskReminderEngine:
    """User-managed reminders. The scheduler only delivers local notifications."""

    def __init__(
        self,
        halt: HaltController,
        audit: AuditLog,
        *,
        db_path: Path,
        notifier: Notifier | None = None,
        clock=None,
        wall_clock=None,
        start_scheduler: bool = True,
        tick_s: float = _TICK_S,
    ) -> None:
        self._halt = halt
        self._audit = audit
        self._store = ReminderStore(db_path)
        self._notifier = notifier or QueueNotifier()
        self._clock = clock or _monotonic
        self._wall = wall_clock or _utcnow
        self._tick_s = max(0.2, float(tick_s))
        self._enabled = False
        self._lock = threading.RLock()
        self._pending: dict | None = None
        self._used: set[str] = set()
        self._notice = ""
        self._suspended = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recover_on_start()
        if start_scheduler:
            self._thread = threading.Thread(target=self._loop, name="jarvis-reminders", daemon=True)
            self._thread.start()

    def status(self) -> dict:
        with self._lock:
            self._expire_pending_locked()
            self._refresh_statuses_locked()
            return self._view_locked()

    def set_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._cancel_pending_locked()
                self._notice = "Tasks and reminders are off."
            else:
                self._notice = "Tasks and reminders are on."
            self._audit.write(
                status="reminders_permission",
                outcome="completed",
                detail="Reminders enabled" if self._enabled else "Reminders disabled",
            )
            return self._enabled

    def create(self, payload: object) -> dict:
        with self._lock:
            if not self._enabled:
                return self._fail("Tasks and reminders are not enabled.")
            if self._halt.halted:
                return self._fail("The action was cancelled.")
            try:
                row = _parse_create(payload, self._wall())
            except _TaskError as exc:
                self._audit.write(status="reminder_rejected", outcome="rejected", detail="Reminder rejected")
                return self._fail(exc.text)
            self._store.upsert(row)
            self._notice = "Reminder created."
            self._audit.write(status="reminder_created", outcome="completed", detail="Reminder created")
            return self._view_locked()

    def request_edit(self, reminder_id: object, payload: object) -> dict:
        with self._lock:
            if not self._enabled:
                return self._fail("Tasks and reminders are not enabled.")
            row = self._require_active(reminder_id)
            if isinstance(row, dict) and "text" in row and "id" not in row:
                return row
            try:
                patch = _parse_edit(payload, row, self._wall())
            except _TaskError as exc:
                return self._fail(exc.text)
            return self._queue_locked("edit", reminder_id=row["id"], patch=patch, summary=f"Edit reminder: {row['title']}")

    def request_cancel(self, reminder_id: object) -> dict:
        with self._lock:
            if not self._enabled:
                return self._fail("Tasks and reminders are not enabled.")
            row = self._require_active(reminder_id)
            if isinstance(row, dict) and "text" in row and "id" not in row:
                return row
            return self._queue_locked("cancel", reminder_id=row["id"], patch={}, summary=f"Cancel reminder: {row['title']}")

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_pending_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked()
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
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
                self._audit.write(status="reminder_denied", outcome="denied", detail="Reminder change denied")
                return self._fail("The action was denied.")
            if not self._enabled:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                return self._fail("Tasks and reminders are not enabled.")
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                return self._fail("The action was cancelled.")
            kind = pending["kind"]
            reminder_id = pending["reminder_id"]
            patch = dict(pending.get("patch") or {})
            self._consume_locked(action_id)
            row = self._store.get(reminder_id)
            if row is None or row["status"] not in _ACTIVE:
                return self._fail("That reminder is not available.")
            now = self._wall()
            if kind == "cancel":
                row["status"] = "cancelled"
                row["updated_at"] = _iso(now)
                row["snooze_until"] = None
                self._store.upsert(row)
                self._notice = "Reminder cancelled."
                self._audit.write(status="reminder_cancelled", outcome="completed", detail="Reminder cancelled")
                return self._view_locked()
            if kind == "edit":
                row.update(patch)
                row["updated_at"] = _iso(now)
                row["missed"] = False
                if row["status"] == "due":
                    row["status"] = "pending"
                self._store.upsert(row)
                self._notice = "Reminder updated."
                self._audit.write(status="reminder_edited", outcome="completed", detail="Reminder edited")
                return self._view_locked()
            return self._fail("That action is not waiting for approval.")

    def complete(self, reminder_id: object) -> dict:
        with self._lock:
            if not self._enabled:
                return self._fail("Tasks and reminders are not enabled.")
            row = self._require_active(reminder_id)
            if isinstance(row, dict) and "text" in row and "id" not in row:
                return row
            now = self._wall()
            row["status"] = "completed"
            row["completed_at"] = _iso(now)
            row["updated_at"] = _iso(now)
            row["snooze_until"] = None
            self._store.upsert(row)
            self._notice = "Reminder completed."
            self._audit.write(status="reminder_completed", outcome="completed", detail="Reminder completed")
            return self._view_locked()

    def snooze(self, reminder_id: object, duration: object) -> dict:
        with self._lock:
            if not self._enabled:
                return self._fail("Tasks and reminders are not enabled.")
            if duration not in _SNOOZE:
                return self._fail("That snooze duration is not allowed.")
            row = self._require_active(reminder_id)
            if isinstance(row, dict) and "text" in row and "id" not in row:
                return row
            now = self._wall()
            until = now + _SNOOZE[str(duration)]
            row["status"] = "snoozed"
            row["snooze_until"] = _iso(until)
            row["due_at"] = _iso(until)
            row["missed"] = False
            row["updated_at"] = _iso(now)
            self._store.upsert(row)
            self._notice = "Reminder snoozed."
            self._audit.write(status="reminder_snoozed", outcome="completed", detail="Reminder snoozed")
            return self._view_locked()

    def tick(self) -> int:
        """Process due reminders once. Used by the scheduler and tests."""
        if self._halt.halted or self._suspended:
            return 0
        with self._lock:
            if not self._enabled:
                return 0
            now = self._wall()
            delivered = 0
            for row in self._store.list_active():
                effective = _effective_due(row)
                if effective is None or effective > now:
                    continue
                occurrence = _iso(effective)
                if self._store.has_delivery(row["id"], occurrence):
                    continue
                if self._halt.halted or self._suspended:
                    break
                kind = "delivered"
                notice = Notice(
                    reminder_id=row["id"],
                    title=row["title"],
                    due_at=occurrence,
                    kind=kind,
                    at=_iso(now),
                )
                if not self._store.record_delivery(row["id"], occurrence, _iso(now), kind):
                    continue
                ok = False
                try:
                    ok = bool(self._notifier.notify(notice))
                except Exception:
                    ok = False
                if not ok:
                    # Delivery failed after booking the occurrence — leave as due/missed, no retry storm.
                    row["status"] = "due"
                    row["missed"] = True
                    row["updated_at"] = _iso(now)
                    self._store.upsert(row)
                    continue
                row["last_delivered_for"] = occurrence
                row["missed"] = False
                row["updated_at"] = _iso(now)
                if row["recurrence"] in {"daily", "weekly"}:
                    nxt = _next_occurrence(row, effective)
                    if nxt is None:
                        row["status"] = "completed"
                        row["completed_at"] = _iso(now)
                        row["snooze_until"] = None
                    else:
                        row["due_at"] = _iso(nxt)
                        row["status"] = "pending"
                        row["snooze_until"] = None
                else:
                    row["status"] = "due"
                self._store.upsert(row)
                delivered += 1
                self._audit.write(status="reminder_delivered", outcome="completed", detail="Reminder notified")
            return delivered

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted:
                self._suspended = True
                self._cancel_pending_locked()
                self._mark_missed_during_stop_locked()
                self._notice = ""

    def resume_delivery(self) -> None:
        """Clear suspension after Emergency Stop. Does not replay deliveries."""
        with self._lock:
            self._suspended = False
            self._mark_missed_during_stop_locked()
            self._notice = "Reminder delivery resumed. Missed reminders were not replayed."

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.tick()
            except Exception:
                continue

    def _recover_on_start(self) -> None:
        with self._lock:
            self._mark_missed_during_stop_locked()

    def _mark_missed_during_stop_locked(self) -> None:
        now = self._wall()
        for row in self._store.list_active():
            effective = _effective_due(row)
            if effective is None or effective > now:
                continue
            occurrence = _iso(effective)
            if self._store.has_delivery(row["id"], occurrence):
                continue
            if self._store.record_delivery(row["id"], occurrence, _iso(now), "missed"):
                row["status"] = "due"
                row["missed"] = True
                row["last_delivered_for"] = occurrence
                row["updated_at"] = _iso(now)
                self._store.upsert(row)
                try:
                    self._notifier.notify(
                        Notice(
                            reminder_id=row["id"],
                            title=row["title"],
                            due_at=occurrence,
                            kind="missed",
                            at=_iso(now),
                        )
                    )
                except Exception:
                    pass
                self._audit.write(status="reminder_missed", outcome="completed", detail="Reminder marked missed")

    def _refresh_statuses_locked(self) -> None:
        now = self._wall()
        for row in self._store.list_active():
            if row["status"] == "snoozed" and row.get("snooze_until"):
                until = _parse_iso(row["snooze_until"])
                if until is not None and until <= now:
                    row["status"] = "pending"
                    row["snooze_until"] = None
                    row["updated_at"] = _iso(now)
                    self._store.upsert(row)
            effective = _effective_due(row)
            if row["status"] == "pending" and effective is not None and effective <= now:
                row["status"] = "due"
                row["updated_at"] = _iso(now)
                self._store.upsert(row)

    def _require_active(self, reminder_id: object) -> dict:
        if not isinstance(reminder_id, str) or not _ID.fullmatch(reminder_id):
            return self._fail("That reminder is not available.")
        row = self._store.get(reminder_id)
        if row is None or row["status"] not in _ACTIVE:
            return self._fail("That reminder is not available.")
        return row

    def _queue_locked(self, kind: str, *, reminder_id: str, patch: dict, summary: str) -> dict:
        if self._pending is not None:
            return self._fail("A reminder change is already waiting.")
        if self._halt.halted:
            return self._fail("The action was cancelled.")
        generation = self._halt.capture()
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
            "kind": kind,
            "reminder_id": reminder_id,
            "patch": patch,
            "summary": summary,
            "generation": generation,
            "expires": self._clock() + 120.0,
        }
        self._notice = summary
        self._audit.write(status="reminder_confirm", outcome="pending", detail="Reminder change waiting")
        return self._view_locked()

    def _expire_pending_locked(self) -> str | None:
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
        now = self._wall()
        upcoming = []
        overdue = []
        for row in self._store.list_all(limit=100):
            public = _public(row)
            if row["status"] in {"completed", "cancelled"}:
                continue
            effective = _effective_due(row)
            if effective is not None and effective <= now and row["status"] in _ACTIVE:
                overdue.append(public)
            elif row["status"] in _ACTIVE:
                upcoming.append(public)
        pending = None
        if self._pending is not None:
            pending = {
                "action_id": self._pending["action_id"],
                "kind": self._pending["kind"],
                "summary": self._pending["summary"],
                "state": "waiting for approval",
            }
        notifications = []
        if isinstance(self._notifier, QueueNotifier):
            notifications = self._notifier.recent(20)
        return {
            "ok": True,
            "permission_enabled": self._enabled,
            "suspended": self._suspended or self._halt.halted,
            "timezone": _local_tz_name(),
            "text": self._notice,
            "pending": pending,
            "upcoming": upcoming,
            "overdue": overdue,
            "notifications": notifications,
            "snooze_options": list(_SNOOZE.keys()),
        }


def _parse_create(payload: object, now: datetime) -> dict:
    if not isinstance(payload, dict):
        raise _TaskError("That reminder is not valid.")
    allowed = {"title", "notes", "due_at", "timezone", "recurrence", "recurrence_until"}
    if set(payload) - allowed:
        raise _TaskError("That reminder is not valid.")
    title = payload.get("title", "")
    notes = payload.get("notes", "")
    due_raw = payload.get("due_at", "")
    tz_name = payload.get("timezone") or _local_tz_name()
    recurrence = payload.get("recurrence", "none")
    until_raw = payload.get("recurrence_until")
    if not isinstance(title, str) or not _TITLE.fullmatch(title) or looks_like_secret(title):
        raise _TaskError("That reminder title is not allowed.")
    if not isinstance(notes, str) or (notes and not _NOTES.fullmatch(notes)) or looks_like_secret(notes):
        raise _TaskError("That reminder note is not allowed.")
    if recurrence not in _RECURRENCE:
        raise _TaskError("That recurrence rule is not allowed.")
    tz = _tz(tz_name)
    due_local = _parse_local(due_raw, tz)
    if due_local <= now.astimezone(tz):
        raise _TaskError("Choose a future date and time.")
    due_utc = due_local.astimezone(timezone.utc)
    until_utc = None
    if recurrence != "none":
        if not isinstance(until_raw, str) or not until_raw:
            raise _TaskError("Recurring reminders need an end date.")
        until_local = _parse_local(until_raw, tz)
        until_utc = until_local.astimezone(timezone.utc)
        if until_utc <= due_utc:
            raise _TaskError("The recurrence end must be after the first due time.")
        if until_utc - due_utc > timedelta(days=_MAX_RECURRENCE_DAYS):
            raise _TaskError("Recurrence cannot exceed 365 days.")
    elif until_raw not in {None, ""}:
        raise _TaskError("That reminder is not valid.")
    stamp = _iso(now)
    return {
        "id": uuid.uuid4().hex,
        "title": title,
        "notes": notes or "",
        "timezone": str(tz),
        "due_at": _iso(due_utc),
        "status": "pending",
        "recurrence": recurrence,
        "recurrence_until": _iso(until_utc) if until_utc else None,
        "snooze_until": None,
        "last_delivered_for": None,
        "missed": False,
        "created_at": stamp,
        "updated_at": stamp,
        "completed_at": None,
    }


def _parse_edit(payload: object, existing: dict, now: datetime) -> dict:
    if not isinstance(payload, dict):
        raise _TaskError("That reminder is not valid.")
    allowed = {"title", "notes", "due_at", "timezone", "recurrence", "recurrence_until"}
    if not payload or set(payload) - allowed:
        raise _TaskError("That reminder is not valid.")
    title = payload.get("title", existing["title"])
    notes = payload.get("notes", existing["notes"])
    if not isinstance(title, str) or not _TITLE.fullmatch(title) or looks_like_secret(title):
        raise _TaskError("That reminder title is not allowed.")
    if not isinstance(notes, str) or (notes and not _NOTES.fullmatch(notes)) or looks_like_secret(notes):
        raise _TaskError("That reminder note is not allowed.")
    schedule_keys = {"due_at", "timezone", "recurrence", "recurrence_until"}
    if not (set(payload) & schedule_keys):
        return {
            "title": title,
            "notes": notes or "",
            "timezone": existing["timezone"],
            "due_at": existing["due_at"],
            "recurrence": existing["recurrence"],
            "recurrence_until": existing.get("recurrence_until"),
            "status": "pending" if existing["status"] != "due" else "due",
            "snooze_until": existing.get("snooze_until"),
        }
    merged = {
        "title": title,
        "notes": notes or "",
        "due_at": payload.get("due_at", existing["due_at"]),
        "timezone": payload.get("timezone", existing["timezone"]),
        "recurrence": payload.get("recurrence", existing["recurrence"]),
        "recurrence_until": payload.get("recurrence_until", existing.get("recurrence_until")),
    }
    created = _parse_create(merged, now)
    return {
        "title": created["title"],
        "notes": created["notes"],
        "timezone": created["timezone"],
        "due_at": created["due_at"],
        "recurrence": created["recurrence"],
        "recurrence_until": created["recurrence_until"],
        "status": "pending",
        "snooze_until": None,
    }


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "notes": row.get("notes") or "",
        "timezone": row["timezone"],
        "due_at": row["due_at"],
        "status": row["status"],
        "recurrence": row["recurrence"],
        "recurrence_until": row.get("recurrence_until"),
        "snooze_until": row.get("snooze_until"),
        "missed": bool(row.get("missed")),
        "last_delivered_for": row.get("last_delivered_for"),
    }


def _effective_due(row: dict) -> datetime | None:
    if row["status"] == "snoozed" and row.get("snooze_until"):
        return _parse_iso(row["snooze_until"])
    return _parse_iso(row.get("due_at"))


def _next_occurrence(row: dict, current: datetime) -> datetime | None:
    if row["recurrence"] == "daily":
        nxt = current + timedelta(days=1)
    elif row["recurrence"] == "weekly":
        nxt = current + timedelta(days=7)
    else:
        return None
    until = _parse_iso(row.get("recurrence_until")) if row.get("recurrence_until") else None
    if until is not None and nxt > until:
        return None
    return nxt


def _parse_local(value: object, tz: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _TaskError("That date and time is not valid.")
    text = value.strip()
    # Accept aware ISO or naive local "YYYY-MM-DDTHH:MM".
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _TaskError("That date and time is not valid.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _tz(name: object) -> ZoneInfo:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise _TaskError("That timezone is not valid.")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise _TaskError("That timezone is not valid.") from exc


def _local_tz_name() -> str:
    import os

    for candidate in (os.environ.get("TZ", "").strip(), "UTC"):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError:
            continue
    return "UTC"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    import time

    return time.monotonic()


class _TaskError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
