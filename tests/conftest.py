"""Give the existing UI tests the per-process token without putting it in URLs."""

from __future__ import annotations

import sys

import fastapi.testclient as _fastapi_testclient
import pytest
from starlette.testclient import TestClient as _RawTestClient


class TestClient(_RawTestClient):
    def __init__(self, app, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        state = getattr(app, "state", None)
        token = getattr(state, "api_token", "")
        if token:
            headers.setdefault("X-Jarvis-Token", token)
        port = 8765
        settings = getattr(state, "settings", None)
        if settings is not None:
            port = settings.port
        kwargs.setdefault("base_url", f"http://127.0.0.1:{port}")
        super().__init__(app, *args, headers=headers, **kwargs)


_fastapi_testclient.TestClient = TestClient


@pytest.fixture(autouse=True)
def _non_windows_test_protector(monkeypatch):
    """Cloud/Linux hosts have no DPAPI; inject the test protector unless a test opts out.

    Production still fails closed without an injected protector on non-Windows.
    Windows keeps the real default so DPAPI round-trip tests stay meaningful.
    """
    if sys.platform == "win32":
        return
    from jarvis.integrations.protect import MemoryProtector

    def _memory() -> MemoryProtector:
        return MemoryProtector()

    monkeypatch.setattr("jarvis.integrations.protect.default_protector", _memory)
    monkeypatch.setattr("jarvis.integrations.store.default_protector", _memory)
