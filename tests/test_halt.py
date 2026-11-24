import httpx

from jarvis.safety.halt import HaltController, HaltedError


def test_stop_drops_queue_and_cancels_approval_without_replay():
    halt = HaltController()
    halt.enqueue({"id": "task-1"})
    halt.request_approval("send-mail")
    report = halt.emergency_stop()
    assert halt.halted is True
    assert halt.status == "stopped"
    assert halt.queue == []
    assert halt.approval_state("send-mail") == "cancelled"
    assert {item["outcome"] for item in report["outcomes"]} == {"cancelled"}
    halt.resume()
    assert halt.halted is False
    assert halt.status == "idle"
    assert halt.queue == []
    assert halt.approval_state("send-mail") == "cancelled"


def test_stop_rejects_new_work_and_ignores_later_status():
    halt = HaltController()
    token = halt.capture()
    halt.emergency_stop()
    assert halt.is_current(token) is False
    with pytest_raises():
        halt.enqueue({"id": "later"})
    halt.set_status("error", "timed out")
    assert halt.status == "stopped"
    halt.set_status("completed", "done")
    assert halt.status == "stopped"


def pytest_raises():
    import pytest

    return pytest.raises(HaltedError)


def test_new_process_does_not_keep_the_previous_queue():
    previous = HaltController()
    previous.enqueue({"id": "old"})
    previous.request_approval("delete-file")
    previous.emergency_stop()
    fresh = HaltController()
    assert fresh.queue == []
    assert fresh.approvals == {}
    assert fresh.halted is False
    assert fresh.status == "idle"


def test_outbound_block_and_already_completed_response():
    halt = HaltController()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    halt.emergency_stop()
    blocked = halt.guarded_post(
        "https://example.test/chat",
        headers={},
        json_body={"text": "hi"},
        timeout=5,
        transport=transport,
    )
    assert blocked.outcome == "cancelled"
    assert calls["n"] == 0

    halt.resume()

    def stopping_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        halt.emergency_stop()
        return httpx.Response(200, json={"ok": True})

    sent = halt.guarded_post(
        "https://example.test/chat",
        headers={},
        json_body={"text": "hi"},
        timeout=5,
        transport=httpx.MockTransport(stopping_handler),
    )
    assert sent.outcome == "already_completed"
    assert calls["n"] == 1


def test_child_process_is_terminated():
    halt = HaltController()

    class Child:
        def __init__(self) -> None:
            self.stopped = False

        def terminate(self) -> None:
            self.stopped = True

    child = Child()
    halt.register_child(child)
    halt.emergency_stop()
    assert child.stopped is True
