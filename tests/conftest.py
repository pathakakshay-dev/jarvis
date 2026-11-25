"""Give the existing UI tests the per-process token without putting it in URLs."""

from __future__ import annotations

import fastapi.testclient as _fastapi_testclient
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
