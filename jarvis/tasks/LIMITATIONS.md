# Task & Reminder Engine — Limitations (Phase 9.2)

- Notifications are local/in-app only in this phase (no system tray toast, no email).
- Recurrence supports `none`, `daily`, and `weekly` only, with a required end date and a 365-day cap.
- Snooze options are fixed: `5m`, `15m`, `1h`, `1d`.
- Scheduler does not execute skills, desktop, browser, or integration actions.
- Emergency Stop suspends delivery; missed items are labeled, not replayed.
- Not connected to Goal Agent, Multi-App, Browser, Desktop, Gmail, or Calendar.
