# Personal Integrations — Limitations (Phase 9.1)

- Google only in this phase (Gmail + Calendar read-only).
- Requires `JARVIS_GOOGLE_CLIENT_ID` and `JARVIS_GOOGLE_CLIENT_SECRET` plus a localhost redirect URI.
- Encrypted token storage uses Windows DPAPI; non-Windows production needs an explicit storage design and is not enabled by default.
- Gmail search returns at most 10 messages; message bodies truncate at 8 KB.
- Calendar loads at most 20 upcoming events within 1–14 days.
- No send, label, archive, delete, event create/edit, or attendee changes.
- No automatic connection to Memory, Goal Agent, Multi-App Orchestrator, browser workflows, or AI providers.
- No background polling or proactive scanning.
