"""Per-process secret for the local UI. Binding to localhost is not authentication."""

from __future__ import annotations

import hashlib
import re
import secrets

from fastapi import Request

TOKEN_HEADER = "x-jarvis-token"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,}")


def new_api_token() -> str:
    return secrets.token_urlsafe(32)


def token_matches(presented: str, expected: str) -> bool:
    if not isinstance(presented, str):
        presented = ""
    if not isinstance(expected, str) or not _TOKEN_RE.fullmatch(expected):
        return False
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return secrets.compare_digest(left, right)


def host_allowed(value: str | None, port: int) -> bool:
    if not value or any(char in value for char in "\r\n@ "):
        return False
    host = value.strip().lower()
    if host.startswith("["):
        return False
    if ":" not in host:
        return False
    name, _, raw_port = host.rpartition(":")
    if raw_port != str(port) or name not in {"127.0.0.1", "localhost"}:
        return False
    return True


def origin_allowed(origin: str | None, port: int) -> bool:
    if origin is None or origin == "":
        return True
    allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    return origin in allowed


def site_allowed(value: str | None) -> bool:
    if value is None or value == "":
        return True
    return value in {"same-origin", "none"}


def local_page_allowed(request: Request, port: int) -> bool:
    if not host_allowed(request.headers.get("host"), port):
        return False
    return origin_allowed(request.headers.get("origin"), port)


def api_allowed(request: Request, token: str, port: int) -> bool:
    if not local_page_allowed(request, port) or not site_allowed(request.headers.get("sec-fetch-site")):
        return False
    return token_matches(request.headers.get(TOKEN_HEADER, ""), token)
