"""Model-independent Emergency Stop latch."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import httpx


class HaltedError(RuntimeError):
    """Raised when new work is requested after Emergency Stop."""


@dataclass
class OutboundResult:
    outcome: str
    response: httpx.Response | None = None


class HaltController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.halted = False
        self.generation = 0
        self.status = "idle"
        self.detail = ""
        self.queue: list[dict] = []
        self.approvals: dict[str, str] = {}
        self.outcomes: list[dict] = []
        self.children: list[Any] = []
        self._clients: list[httpx.Client] = []
        self.busy = False
        self.hotkey_available = False
        self.hotkey_note = "The Stop button is available."

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "detail": self.detail,
                "halted": self.halted,
                "generation": self.generation,
                "hotkey_available": self.hotkey_available,
                "hotkey_note": self.hotkey_note,
                "outcomes": list(self.outcomes),
                "queue_size": len(self.queue),
                "busy": self.busy,
            }

    def set_status(self, status: str, detail: str = "") -> None:
        with self._lock:
            if self.halted and status != "stopped":
                return
            self.status = status
            self.detail = detail

    def allow_outbound(self) -> bool:
        with self._lock:
            return not self.halted

    def capture(self) -> int:
        with self._lock:
            return self.generation

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return (not self.halted) and self.generation == generation

    def try_begin(self) -> bool:
        with self._lock:
            if self.halted or self.busy:
                return False
            self.busy = True
            return True

    def finish_busy(self) -> None:
        with self._lock:
            self.busy = False

    def enqueue(self, item: dict) -> None:
        with self._lock:
            if self.halted:
                raise HaltedError("stopped")
            self.queue.append(dict(item))

    def request_approval(self, action_id: str) -> None:
        with self._lock:
            if self.halted:
                raise HaltedError("stopped")
            self.approvals[action_id] = "pending"

    def approval_state(self, action_id: str) -> str | None:
        with self._lock:
            return self.approvals.get(action_id)

    def resolve_approval(self, action_id: str, decision: str) -> bool:
        """Record one pending action as approved or denied. Stopped actions stay cancelled."""
        if decision not in {"approved", "denied"}:
            raise ValueError("unknown approval decision")
        with self._lock:
            if self.halted or self.approvals.get(action_id) != "pending":
                return False
            self.approvals[action_id] = decision
            return True

    def register_child(self, proc: Any) -> None:
        with self._lock:
            self.children.append(proc)

    def register_client(self, client: httpx.Client) -> None:
        with self._lock:
            self._clients.append(client)

    def unregister_client(self, client: httpx.Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def emergency_stop(self) -> dict:
        with self._lock:
            self.halted = True
            self.generation += 1
            self.status = "stopped"
            self.detail = "Stopped"
            dropped = list(self.queue)
            self.queue.clear()
            cancelled = [key for key, state in self.approvals.items() if state == "pending"]
            for key in cancelled:
                self.approvals[key] = "cancelled"
            clients = list(self._clients)
            children = list(self.children)
        outcomes: list[dict] = []
        for item in dropped:
            outcomes.append({"action": str(item.get("id", "queued")), "outcome": "cancelled"})
        for key in cancelled:
            outcomes.append({"action": key, "outcome": "cancelled"})
        for client in clients:
            try:
                client.close()
                outcomes.append({"action": "network", "outcome": "cancelled"})
            except Exception:
                outcomes.append({"action": "network", "outcome": "not_confirmed"})
        for proc in children:
            try:
                proc.terminate()
                outcomes.append({"action": "child", "outcome": "cancelled"})
            except Exception:
                outcomes.append({"action": "child", "outcome": "not_confirmed"})
        with self._lock:
            self.outcomes.extend(outcomes)
        return {"halted": True, "outcomes": outcomes}

    def resume(self) -> None:
        """Clear the latch only. Dropped work is not replayed."""
        with self._lock:
            self.halted = False
            self.status = "idle"
            self.detail = ""
            self.outcomes.clear()

    def guarded_post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> OutboundResult:
        if not self.allow_outbound():
            return OutboundResult(outcome="cancelled")
        client = httpx.Client(timeout=timeout, transport=transport)
        self.register_client(client)
        try:
            if not self.allow_outbound():
                return OutboundResult(outcome="cancelled")
            response = client.post(url, headers=headers, json=json_body)
            if not self.allow_outbound():
                return OutboundResult(outcome="already_completed", response=response)
            return OutboundResult(outcome="ok", response=response)
        except httpx.TimeoutException:
            if not self.allow_outbound():
                return OutboundResult(outcome="cancelled")
            return OutboundResult(outcome="timeout")
        except httpx.HTTPError:
            if not self.allow_outbound():
                return OutboundResult(outcome="cancelled")
            return OutboundResult(outcome="http_error")
        finally:
            self.unregister_client(client)
            try:
                client.close()
            except Exception:
                pass

    def guarded_get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> OutboundResult:
        if not self.allow_outbound():
            return OutboundResult(outcome="cancelled")
        client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self.register_client(client)
        try:
            if not self.allow_outbound():
                return OutboundResult(outcome="cancelled")
            response = client.get(url, headers=headers, params=params)
            if not self.allow_outbound():
                return OutboundResult(outcome="already_completed", response=response)
            return OutboundResult(outcome="ok", response=response)
        except httpx.TimeoutException:
            if not self.allow_outbound():
                return OutboundResult(outcome="cancelled")
            return OutboundResult(outcome="timeout")
        except httpx.HTTPError:
            if not self.allow_outbound():
                return OutboundResult(outcome="cancelled")
            return OutboundResult(outcome="http_error")
        finally:
            self.unregister_client(client)
            try:
                client.close()
            except Exception:
                pass
