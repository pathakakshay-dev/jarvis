"""Gmail read-only adapter. Search and fetch only; no send/modify."""

from __future__ import annotations

import base64
import re
import threading
import uuid
from email.header import decode_header, make_header

from jarvis.audit import AuditLog
from jarvis.integrations.limits import RateLimiter
from jarvis.integrations.oauth_google import GMAIL_SCOPE
from jarvis.safety.halt import HaltController, HaltedError
from jarvis.safety.secrets import looks_like_secret

_TIMEOUT_S = 20.0
_MAX_RESULTS = 10
_MAX_QUERY = 200
_MAX_BODY = 8000
_MAX_SNIPPET = 240
_ID = re.compile(r"^[0-9a-f]{32}$")
_MSG_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_QUERY = re.compile(r"^[A-Za-z0-9 @\-_\.\"':()/]+$")
_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/"


class GmailIntegration:
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
                self._notice = "Gmail read is off."
            else:
                self._notice = "Gmail read is on. Search only when you ask."
            return self._enabled

    def request_search(self, query: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._fail("The action was cancelled.")
            if not self._enabled:
                self._audit.write(status="gmail_rejected", outcome="rejected", detail="Gmail read is not enabled")
                return self._fail("Gmail read is not enabled.")
            if not isinstance(query, str):
                return self._fail("That Gmail search is not valid.")
            cleaned = " ".join(query.split())
            if not cleaned or len(cleaned) > _MAX_QUERY or not _QUERY.fullmatch(cleaned) or looks_like_secret(cleaned):
                self._audit.write(status="gmail_rejected", outcome="rejected", detail="Gmail query rejected")
                return self._fail("That Gmail search is not allowed.")
            return self._queue_locked("search", {"query": cleaned}, f"Search Gmail for: {cleaned}")

    def request_message(self, message_id: object) -> dict:
        with self._lock:
            self._expire_locked()
            if self._halt.halted:
                return self._fail("The action was cancelled.")
            if not self._enabled:
                return self._fail("Gmail read is not enabled.")
            if not isinstance(message_id, str) or not _MSG_ID.fullmatch(message_id) or len(message_id) > 128:
                return self._fail("That Gmail message is not available.")
            return self._queue_locked("message", {"message_id": message_id}, "Read one Gmail message.")

    def confirm(self, action_id: object, approved: object) -> dict:
        with self._lock:
            expired = self._expire_locked()
            if isinstance(action_id, str) and expired == action_id:
                return self._view_locked()
            if not isinstance(action_id, str) or not _ID.fullmatch(action_id) or action_id in self._used:
                self._audit.write(status="gmail_rejected", outcome="rejected", detail="Gmail action rejected")
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
                self._audit.write(status="gmail_denied", outcome="denied", detail="Gmail action denied")
                return self._fail("The action was denied.")
            if not self._enabled:
                self._halt.resolve_approval(action_id, "denied")
                self._consume_locked(action_id)
                return self._fail("Gmail read is not enabled.")
            if not self._halt.resolve_approval(action_id, "approved"):
                self._consume_locked(action_id)
                return self._fail("The action was cancelled.")
            kind = pending["kind"]
            args = dict(pending["args"])
            generation = pending["generation"]
            self._consume_locked(action_id)

        if not self._limiter.allow():
            with self._lock:
                return self._fail("Gmail rate limit reached. Try again later.")
        tokens = self._store.load()
        if tokens is None:
            with self._lock:
                return self._fail("Connect Google before reading Gmail.")
        if GMAIL_SCOPE not in tokens.get("scopes", []):
            with self._lock:
                return self._fail("Google did not grant Gmail read access.")
        refreshed = self._oauth.refresh_if_needed(tokens)
        if refreshed is None:
            with self._lock:
                return self._fail("Google sign-in expired. Connect again.")
        if not self._halt.is_current(generation):
            with self._lock:
                return self._fail("The action was cancelled.")
        try:
            if kind == "search":
                rows = self._search(refreshed["access_token"], args["query"], generation)
                with self._lock:
                    self._last = {"kind": "search", "query": args["query"], "messages": rows}
                    self._notice = f"Found {len(rows)} Gmail message(s)."
                    self._audit.write(status="gmail_search", outcome="completed", detail="Gmail search completed")
                    return self._view_locked()
            message = self._message(refreshed["access_token"], args["message_id"], generation)
            with self._lock:
                self._last = {"kind": "message", "message": message}
                self._notice = "Gmail message loaded."
                self._audit.write(status="gmail_message", outcome="completed", detail="Gmail message read")
                return self._view_locked()
        except _GmailError as exc:
            with self._lock:
                self._audit.write(status="gmail_failed", outcome="failed", detail="Gmail request failed")
                return self._fail(exc.text)
        except Exception:
            with self._lock:
                self._audit.write(status="gmail_failed", outcome="failed", detail="Gmail request failed")
                return self._fail("Gmail request failed.")

    def clear_result(self) -> dict:
        with self._lock:
            self._last = None
            self._notice = "Gmail results cleared."
            return self._view_locked()

    def observe_stop(self) -> None:
        with self._lock:
            if self._halt.halted or (self._pending is not None and not self._halt.is_current(self._pending["generation"])):
                self._cancel_pending_locked()
                self._last = None
                self._notice = ""

    def _queue_locked(self, kind: str, args: dict, summary: str) -> dict:
        if self._pending is not None:
            return self._fail("A Gmail action is already waiting.")
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
            "kind": kind,
            "args": args,
            "summary": summary,
            "generation": generation,
            "expires": self._clock() + self._ttl_s,
        }
        self._notice = summary
        self._audit.write(status="gmail_requested", outcome="pending", detail="Gmail action requested")
        return self._view_locked()

    def _search(self, access_token: str, query: str, generation: int) -> list[dict]:
        result = self._halt.guarded_get(
            _LIST_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={"q": query, "maxResults": str(_MAX_RESULTS)},
            timeout=_TIMEOUT_S,
            transport=self._transport,
        )
        if not self._halt.is_current(generation) or result.outcome in {"cancelled", "already_completed"}:
            raise _GmailError("The action was cancelled.")
        if result.outcome != "ok" or result.response is None or result.response.status_code != 200:
            raise _GmailError("Gmail search failed.")
        try:
            payload = result.response.json()
        except ValueError as exc:
            raise _GmailError("Gmail returned invalid data.") from exc
        if not isinstance(payload, dict):
            raise _GmailError("Gmail returned invalid data.")
        rows = payload.get("messages") or []
        if not isinstance(rows, list):
            raise _GmailError("Gmail returned invalid data.")
        out = []
        for item in rows[:_MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id")
            thread_id = item.get("threadId", "")
            if not isinstance(message_id, str) or not _MSG_ID.fullmatch(message_id):
                continue
            if thread_id and (not isinstance(thread_id, str) or not _MSG_ID.fullmatch(thread_id)):
                continue
            meta = self._metadata(access_token, message_id, generation)
            out.append(meta)
        return out

    def _metadata(self, access_token: str, message_id: str, generation: int) -> dict:
        result = self._halt.guarded_get(
            _GET_URL + message_id,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "To"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date"),
            ],
            timeout=_TIMEOUT_S,
            transport=self._transport,
        )
        if not self._halt.is_current(generation) or result.outcome != "ok" or result.response is None or result.response.status_code != 200:
            return {"id": message_id, "subject": "", "from": "", "date": "", "snippet": ""}
        try:
            payload = result.response.json()
        except ValueError:
            return {"id": message_id, "subject": "", "from": "", "date": "", "snippet": ""}
        return _public_message_meta(payload, message_id)

    def _message(self, access_token: str, message_id: str, generation: int) -> dict:
        result = self._halt.guarded_get(
            _GET_URL + message_id,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={"format": "full"},
            timeout=_TIMEOUT_S,
            transport=self._transport,
        )
        if not self._halt.is_current(generation) or result.outcome in {"cancelled", "already_completed"}:
            raise _GmailError("The action was cancelled.")
        if result.outcome != "ok" or result.response is None or result.response.status_code != 200:
            raise _GmailError("Gmail message read failed.")
        try:
            payload = result.response.json()
        except ValueError as exc:
            raise _GmailError("Gmail returned invalid data.") from exc
        if not isinstance(payload, dict):
            raise _GmailError("Gmail returned invalid data.")
        meta = _public_message_meta(payload, message_id)
        body = _extract_body(payload)
        if len(body) > _MAX_BODY:
            body = body[:_MAX_BODY] + "\n[truncated]"
        meta["body"] = body
        return meta

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
                "kind": self._pending["kind"],
                "state": "waiting for approval",
            }
        result = None
        if self._last is not None:
            result = self._last
        return {
            "ok": self._pending is not None or self._last is not None,
            "permission_enabled": self._enabled,
            "text": self._notice,
            "pending": pending,
            "result": result,
        }


def _public_message_meta(payload: dict, message_id: str) -> dict:
    headers = {}
    payload_headers = ((payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}).get("headers") or []
    if isinstance(payload_headers, list):
        for row in payload_headers:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            value = row.get("value")
            if isinstance(name, str) and isinstance(value, str) and name.lower() in {"from", "to", "subject", "date"}:
                headers[name.lower()] = _decode_mime(value)[:300]
    snippet = payload.get("snippet", "")
    if not isinstance(snippet, str):
        snippet = ""
    snippet = snippet[:_MAX_SNIPPET]
    return {
        "id": message_id,
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "snippet": snippet,
    }


def _extract_body(payload: dict) -> str:
    root = payload.get("payload")
    if not isinstance(root, dict):
        return ""
    text = _walk_parts(root)
    return " ".join(text.split())


def _walk_parts(node: dict) -> str:
    mime = str(node.get("mimeType") or "")
    body = node.get("body") if isinstance(node.get("body"), dict) else {}
    data = body.get("data") if isinstance(body, dict) else None
    if mime.startswith("text/plain") and isinstance(data, str) and data:
        try:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts = node.get("parts")
    if isinstance(parts, list):
        chunks = []
        for part in parts:
            if isinstance(part, dict):
                chunk = _walk_parts(part)
                if chunk:
                    chunks.append(chunk)
        return "\n".join(chunks)
    return ""


def _decode_mime(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _monotonic() -> float:
    import time

    return time.monotonic()


class _GmailError(Exception):
    def __init__(self, text: str) -> None:
        self.text = text
