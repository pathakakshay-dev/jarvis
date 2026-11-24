"""Page-level stop text. Stopped is shown only after a confirmed status read."""

from __future__ import annotations

CONFIRM_AFTER_S = 2.0

LABELS = {
    "idle": "Idle",
    "listening": "Listening",
    "thinking": "Thinking",
    "planning": "Planning",
    "requesting_approval": "Requesting approval",
    "executing": "Executing a tool",
    "completed": "Completed",
    "error": "Error",
    "stopped": "Stopped",
}


def banner_for(polled_status: str | None, stop_requested_at: float | None, now: float) -> str:
    """Return the banner text for one poll.

    stop_requested_at is the local clock when the user pressed Emergency Stop.
    None means this page did not press it, so a polled stopped state (for example
    from the hotkey) is already a confirmed read.
    """
    if stop_requested_at is not None:
        if polled_status == "stopped":
            return "Stopped"
        if now - stop_requested_at >= CONFIRM_AFTER_S:
            return "Stop could not be confirmed"
        return "Stopping"
    if polled_status == "stopped":
        return "Stopped"
    if not polled_status:
        return "Stop could not be confirmed"
    return LABELS.get(polled_status, polled_status)
