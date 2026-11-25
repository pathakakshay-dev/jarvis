"""Task & Reminder Engine tests. Mocked clocks and notifications only."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.providers.fake import FakeProvider
from jarvis.tasks.engine import TaskReminderEngine
from jarvis.tasks.notify import QueueNotifier


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="fake",
        groq_api_key="",
        groq_model="openai/gpt-oss-20b",
        gemini_api_key="",
        gemini_model="gemini-2.5-flash",
        timeout_s=5,
        max_steps=4,
        tool_timeout_s=20,
        host="127.0.0.1",
        port=8765,
        data_dir=tmp_path / "data",
        root=tmp_path,
    )


def _app(tmp_path: Path, *, notifier=None, clock=None, wall=None):
    return create_app(
        settings=_settings(tmp_path),
        fake=FakeProvider(),
        start_hotkeys_flag=False,
        transcriber=object(),
        speaker=object(),
        notepad_launcher=lambda *_a, **_k: True,
        calculator_launcher=lambda *_a, **_k: True,
        reminder_notifier=notifier or QueueNotifier(),
        start_reminder_scheduler=False,
    )


def _enable(client: TestClient) -> None:
    assert client.post("/api/reminders/permission", json={"enabled": True}).json()["enabled"] is True


def _future(hours: float = 2, tz: str = "UTC") -> str:
    when = datetime.now(ZoneInfo(tz)) + timedelta(hours=hours)
    return when.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def test_permission_create_complete_cancel_edit(tmp_path: Path):
    notifier = QueueNotifier()
    app = _app(tmp_path, notifier=notifier)
    with TestClient(app) as client:
        assert client.get("/api/reminders").json()["permission_enabled"] is False
        refused = client.post(
            "/api/reminders/create",
            json={"title": "Call bank", "due_at": _future(), "timezone": "UTC", "recurrence": "none"},
        ).json()
        assert "not enabled" in refused["text"]
        _enable(client)
        created = client.post(
            "/api/reminders/create",
            json={"title": "Call bank", "notes": "After lunch", "due_at": _future(), "timezone": "UTC", "recurrence": "none"},
        ).json()
        assert len(created["upcoming"]) == 1
        reminder_id = created["upcoming"][0]["id"]
        secret = client.post(
            "/api/reminders/create",
            json={"title": "password = hunter2", "due_at": _future(), "timezone": "UTC", "recurrence": "none"},
        ).json()
        assert secret.get("upcoming") is None or len(secret["upcoming"]) == 1
        assert "not allowed" in secret["text"]

        past = client.post(
            "/api/reminders/create",
            json={"title": "Too late", "due_at": "2020-01-01T10:00", "timezone": "UTC", "recurrence": "none"},
        ).json()
        assert "future" in past["text"].lower()

        edit = client.post("/api/reminders/edit", json={"reminder_id": reminder_id, "title": "Call office"}).json()
        assert edit["pending"]["kind"] == "edit"
        denied = client.post("/api/reminders/confirm", json={"action_id": edit["pending"]["action_id"], "approved": False}).json()
        assert "denied" in denied["text"].lower()

        edit = client.post("/api/reminders/edit", json={"reminder_id": reminder_id, "title": "Call office"}).json()
        approved = client.post("/api/reminders/confirm", json={"action_id": edit["pending"]["action_id"], "approved": True}).json()
        assert approved["upcoming"][0]["title"] == "Call office"

        cancel = client.post("/api/reminders/cancel", json={"reminder_id": reminder_id}).json()
        done = client.post("/api/reminders/confirm", json={"action_id": cancel["pending"]["action_id"], "approved": True}).json()
        assert done["upcoming"] == []
        assert app.state.fake.calls == 0
        assert "hunter2" not in (app.state.audit.path.read_text(encoding="utf-8") if app.state.audit.path.exists() else "")


def test_recurrence_timezone_snooze_overdue_and_tick(tmp_path: Path):
    notifier = QueueNotifier()
    now = {"t": datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)}

    def wall():
        return now["t"]

    app = _app(tmp_path, notifier=notifier)
    engine: TaskReminderEngine = app.state.reminder_engine
    engine._wall = wall
    with TestClient(app) as client:
        _enable(client)
        # DST spring-forward window in America/New_York; still accepts explicit local times.
        due = "2026-03-08T03:30"
        until = "2026-03-20T03:30"
        created = client.post(
            "/api/reminders/create",
            json={
                "title": "DST check",
                "due_at": due,
                "timezone": "America/New_York",
                "recurrence": "daily",
                "recurrence_until": until,
            },
        ).json()
        assert created["upcoming"][0]["recurrence"] == "daily"
        reminder_id = created["upcoming"][0]["id"]

        # Advance wall clock past first due.
        now["t"] = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
        assert engine.tick() == 1
        assert len(notifier.calls) == 1
        assert notifier.calls[0].kind == "delivered"
        # Duplicate tick must not redeliver same occurrence.
        assert engine.tick() == 0
        state = client.get("/api/reminders").json()
        assert state["upcoming"][0]["id"] == reminder_id
        assert state["upcoming"][0]["status"] == "pending"

        snoozed = client.post("/api/reminders/snooze", json={"reminder_id": reminder_id, "duration": "15m"}).json()
        assert snoozed["upcoming"][0]["status"] == "snoozed"
        bad_snooze = client.post("/api/reminders/snooze", json={"reminder_id": reminder_id, "duration": "3h"}).json()
        assert "not allowed" in bad_snooze["text"]

        # One-time overdue/missed path via recovery.
        before_calls = len(notifier.calls)
        once = client.post(
            "/api/reminders/create",
            json={"title": "Soon", "due_at": "2026-03-08T10:00", "timezone": "UTC", "recurrence": "none"},
        ).json()
        one_id = once["upcoming"][0]["id"]
        now["t"] = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
        app.state.halt.halted = True
        engine.observe_stop()
        assert engine.tick() == 0
        app.state.halt.halted = False
        engine.resume_delivery()
        state = client.get("/api/reminders").json()
        overdue_ids = {row["id"] for row in state["overdue"]}
        assert one_id in overdue_ids
        assert any(row.get("missed") for row in state["overdue"] if row["id"] == one_id)
        later = notifier.calls[before_calls:]
        kinds = [item.kind for item in later if item.reminder_id == one_id]
        assert kinds == ["missed"]
        assert not any(item.kind == "delivered" and item.reminder_id == one_id for item in later)


def test_restart_recovery_concurrent_and_persistence(tmp_path: Path):
    notifier = QueueNotifier()
    db = tmp_path / "data" / "reminders.sqlite3"
    now = {"t": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)}

    def wall():
        return now["t"]

    app = _app(tmp_path, notifier=notifier)
    engine: TaskReminderEngine = app.state.reminder_engine
    engine._wall = wall
    with TestClient(app) as client:
        _enable(client)
        created = client.post(
            "/api/reminders/create",
            json={"title": "Persist me", "due_at": "2026-06-01T15:00", "timezone": "UTC", "recurrence": "none"},
        ).json()
        reminder_id = created["upcoming"][0]["id"]
        assert db.is_file()

    # Simulate restart with a due reminder that was never delivered.
    notifier2 = QueueNotifier()
    now["t"] = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    engine2 = TaskReminderEngine(
        app.state.halt,
        app.state.audit,
        db_path=db,
        notifier=notifier2,
        wall_clock=wall,
        start_scheduler=False,
    )
    state = engine2.status()
    assert any(row["id"] == reminder_id and row.get("missed") for row in state["overdue"])
    assert all(item.kind == "missed" for item in notifier2.calls)
    # Second recovery must not duplicate missed notifications for same occurrence.
    before = len(notifier2.calls)
    engine2._mark_missed_during_stop_locked()
    assert len(notifier2.calls) == before

    # Concurrent ticks must not double-deliver a fresh reminder.
    notifier3 = QueueNotifier()
    now["t"] = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    engine3 = TaskReminderEngine(
        app.state.halt,
        app.state.audit,
        db_path=tmp_path / "data" / "reminders2.sqlite3",
        notifier=notifier3,
        wall_clock=wall,
        start_scheduler=False,
    )
    engine3.set_enabled(True)
    created_race = engine3.create(
        {"title": "Race", "due_at": "2026-06-01T15:30", "timezone": "UTC", "recurrence": "none"}
    )
    assert created_race["upcoming"]
    now["t"] = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    results = []

    def run_tick():
        results.append(engine3.tick())

    threads = [threading.Thread(target=run_tick) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 1
    assert len([item for item in notifier3.calls if item.kind == "delivered"]) == 1


def test_emergency_stop_resume_permission_malformed_leakage(tmp_path: Path):
    notifier = QueueNotifier()
    now = {"t": datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)}

    def wall():
        return now["t"]

    app = _app(tmp_path, notifier=notifier)
    engine: TaskReminderEngine = app.state.reminder_engine
    engine._wall = wall
    with TestClient(app) as client:
        _enable(client)
        created = client.post(
            "/api/reminders/create",
            json={"title": "Stop test", "due_at": "2026-07-01T11:00", "timezone": "UTC", "recurrence": "none"},
        ).json()
        reminder_id = created["upcoming"][0]["id"]
        now["t"] = datetime(2026, 7, 1, 11, 5, tzinfo=timezone.utc)
        client.post("/api/emergency-stop", json={})
        assert engine.tick() == 0
        client.post("/api/resume", json={})
        state = client.get("/api/reminders").json()
        assert any(row["id"] == reminder_id and row.get("missed") for row in state["overdue"])
        delivered = [item for item in notifier.calls if item.reminder_id == reminder_id and item.kind == "delivered"]
        assert delivered == []

        client.post("/api/reminders/permission", json={"enabled": False})
        assert engine.tick() == 0
        bad = client.post(
            "/api/reminders/create",
            json={"title": "Bad", "due_at": "not-a-date", "timezone": "UTC", "recurrence": "none"},
        ).json()
        # permission off still blocks
        assert "not enabled" in bad["text"]
        _enable(client)
        bad_tz = client.post(
            "/api/reminders/create",
            json={"title": "Bad tz", "due_at": _future(), "timezone": "Not/AZone", "recurrence": "none"},
        ).json()
        assert "timezone" in bad_tz["text"].lower()
        long_rec = client.post(
            "/api/reminders/create",
            json={
                "title": "Long",
                "due_at": "2026-07-02T10:00",
                "timezone": "UTC",
                "recurrence": "daily",
                "recurrence_until": "2028-07-02T10:00",
            },
        ).json()
        assert "365" in long_rec["text"]
        body = json.dumps(client.get("/api/reminders").json())
        assert "password" not in body.lower()
        assert app.state.fake.calls == 0
        complete = client.post("/api/reminders/complete", json={"reminder_id": reminder_id}).json()
        assert all(row["id"] != reminder_id for row in complete["overdue"])
        assert all(row["id"] != reminder_id for row in complete["upcoming"])
