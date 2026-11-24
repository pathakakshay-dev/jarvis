from pathlib import Path

from jarvis.audit import scrub
from jarvis.audit import AuditLog
from jarvis.safety.confirm import banner_for
from jarvis.safety.hotkey import KILL_ID, STOP_ID, handle_hotkey
from jarvis.safety.halt import HaltController


def test_banner_waits_for_a_confirmed_status():
    assert banner_for("thinking", 0.0, 0.2) == "Stopping"
    assert banner_for("thinking", 0.0, 2.0) == "Stop could not be confirmed"
    assert banner_for("stopped", 0.0, 0.1) == "Stopped"
    assert banner_for("stopped", None, 0.0) == "Stopped"
    assert banner_for(None, 0.0, 3.0) == "Stop could not be confirmed"
    assert banner_for("thinking", None, 0.0) == "Thinking"


def test_page_does_not_claim_stopped_before_the_status_read():
    page = Path("jarvis/static/index.html").read_text(encoding="utf-8")
    button = page.split('id="emergency-stop"', 1)[0]
    assert "<form" not in button
    assert "disabled" not in page.split('id="emergency-stop"', 1)[1].split(">", 1)[0]
    function = page.split("function bannerFor", 1)[1].split("function renderBanner", 1)[0]
    assert 'polledStatus === "stopped"' in function
    assert 'return "Stopped"' in function
    assert 'return "Stop could not be confirmed"' in function
    assert 'return "Stopping"' in function
    click = page.split('id="emergency-stop"', 1)[1]
    assert click.index("stopRequestedAt = Date.now()") < click.index("/api/emergency-stop")


def test_hotkey_dispatch_does_not_ask_a_model():
    halt = HaltController()
    ended = {"yes": False}

    def end_process() -> None:
        ended["yes"] = True

    handle_hotkey(STOP_ID, halt, end_process)
    assert halt.status == "stopped"
    assert ended["yes"] is False
    handle_hotkey(KILL_ID, halt, end_process)
    assert ended["yes"] is True


def test_audit_redacts_secrets(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write(status="error", detail="api_key=super-secret Authorization: Bearer abc.def")
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "super-secret" not in text
    assert "abc.def" not in text
    assert "redacted" in scrub("bearer sk-live")
