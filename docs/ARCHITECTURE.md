# JARVIS Architecture & Capability Map (Phase 9.3)

Concise map of the running system. Capabilities below exist in the current tree; this document does not describe unbuilt phases.

## Runtime shape

```text
Browser UI (static/index.html)
        │  localhost + per-process API token
        ▼
FastAPI app (jarvis/main.py)  ── host 127.0.0.1
        │
        ├── HaltController / Policy / AuditLog
        ├── Chat + Memory store (SQLite under data/)
        ├── Providers (fake | groq | gemini)
        ├── Skills + SkillExecutor (workspace jail)
        ├── Goal Agent
        ├── Desktop modules (safety, simulator, notepad, calculator,
        │     control, window switch, action engine)
        ├── Browser modules (intelligence, bridge, reader, actions, workflow)
        ├── Multi-App Orchestrator
        ├── Google integrations (OAuth + Gmail + Calendar adapters)
        ├── Task / Reminder engine
        └── RecoveryService (workflow checkpoints)
```

JARVIS is a **local** assistant. Binding to `127.0.0.1` is not authentication by itself; the UI injects a per-process token for `/api/*`, and Host/Origin checks reject foreign sites.

## Capability map

| Capability | Primary modules | User gate |
|------------|-----------------|-----------|
| Chat | `main`, providers, message store | Running + provider selection |
| Controlled memory | memory store, secret checks | Explicit save; confirm clear/delete |
| Push-to-talk voice | Whisper local + Edge TTS / Windows voices | Hold-to-talk; mute default |
| Web research | research path + HTTPS fetch limits | Research button or `research:` prefix |
| File skills | `skills/files` (+ folder) | Permission OFF by default; approval for writes |
| System information | `skills/system` | Permission + approval path |
| Application inventory | `skills/apps` | Permission + approval path |
| Clipboard intelligence | `skills/clipboard` | Permission + approval; not auto-forwarded to AI |
| Desktop planner / simulator | desktop plan + simulator | Simulator is mock-only |
| Notepad / Calculator | `desktop/notepad`, `desktop/calculator` | Permission + per-action approval |
| Desktop control / focus / engine | `desktop/control`, `switch`, `engine` | Separate permissions; revalidation |
| Goal Agent | `agent/goal` | Plan review → Start → per-step approval |
| Browser intelligence | `browser/intelligence` | Permission; title inspection after approval |
| Browser bridge / reader / actions / workflow | `browser/bridge`, `page_text`, `actions`, `workflow` + `extensions/jarvis-browser-bridge` | Independent permissions; extension claim for actions |
| Multi-app orchestrator | `workflow/multi_app` | Own permission; ≤6 steps; nested executor approvals |
| Google Gmail / Calendar (read-only) | `integrations/*` | OAuth connect; per-adapter permission; per-request approval |
| Tasks & reminders | `tasks/*` | Permission; edit/cancel confirmations; notify-only scheduler |
| Durable workflow recovery | `recovery/*` | Startup seals interrupted; UI abandon / restart-begin only |

## Safety architecture

### Local authentication and permissions

- Per-process API token embedded in the local page; API requests must present it.
- Host/Origin limited to `127.0.0.1` / `localhost` for the configured port.
- `Policy` permission flags default to **OFF**. Feature modules also keep their own enable flags (browser, multi-app, reminders, integrations, and so on).

### Per-action approval

- Sensitive skills and desktop/browser/integration actions require an explicit approve/deny.
- Approving a **plan** (Goal, Desktop Engine, Browser Workflow, Multi-App) does **not** approve each nested action.
- Multi-App arms existing executors; those executors retain their own `action_id`s and validation.

### Emergency Stop and cancellation

- Emergency Stop sets the global halt, cancels in-flight guarded work, and clears pending approvals where modules observe stop.
- Resume clears the halt flag only. It does **not** replay messages, approvals, research, notifications, or workflows.
- Module Cancel endpoints permanently cancel the current execution context for that feature.

### Recovery (no automatic resume)

- Goal Agent, Multi-App, and Browser Workflow write privacy-minimal checkpoints.
- On process start, any `active` checkpoint is sealed as `interrupted` / needs review.
- **Never** auto-resume mid-step.
- Recovery UI options: review, **abandon**, or **restart from beginning**.
- Restart proposes a **new** execution from sanitized metadata; old approvals and action IDs are void.
- Bounded read-only Goal retries may re-queue for a **fresh** approval after timeout; write / desktop / browser actions are never auto-retried.
- Corrupt or unknown checkpoint state fails closed.

### Browser and desktop restrictions

- No unrestricted shell / PowerShell / arbitrary code execution skills.
- Browser: no arbitrary JS selectors for free-form automation in this design; no navigation/form-submit/credential flows in the bridge action set; workflows capped (≤5 steps) and require the extension claim model for action steps.
- Desktop: restricted app targets and action sets; simulator does not drive the real desktop; engine/control revalidate targets.
- Multi-App does not invent new desktop/browser primitives.

### Privacy and secret-handling boundaries

- `.env` holds provider and OAuth client secrets locally; never commit it.
- Runtime data lives under `data/` (SQLite, audit log, Whisper cache, workflow checkpoints, DPAPI Google token blob).
- User workspace files stay under `workspace/`.
- Secret-like strings are rejected from memory and many user-facing fields; audit details stay privacy-minimal (for example typed text is not dumped into multi-app/browser audit detail).
- Gmail/Calendar content is fetched only on explicit user request + approval and is not auto-forwarded to chat providers from those panels.
- Reminder scheduler never runs shell, browser, desktop, email, or calendar actions.

## Trust boundaries (short)

1. **Untrusted:** model output, web page text, clipboard text, email/calendar payloads, extension-reported page metadata.
2. **Trusted configuration:** local `.env`, permission flags, HaltController generation.
3. **User authority:** Enable permission, approve action, Start plan, Emergency Stop, recovery abandon/restart.

## Related module notes

Module-level security/limitation notes (already in-repo):

- `jarvis/workflow/SECURITY.md`, `LIMITATIONS.md`
- `jarvis/integrations/SECURITY.md`, `LIMITATIONS.md`
- `jarvis/tasks/SECURITY.md`, `LIMITATIONS.md`
- `jarvis/recovery/SECURITY.md`, `LIMITATIONS.md`
- `extensions/jarvis-browser-bridge/SECURITY.md`, `LIMITATIONS.md`

## Out of scope (not present as product features)

Examples that must not be assumed from this map: unrestricted shell, background browser monitoring, Google send/modify, automatic resume of interrupted workflows, Cloud-hosted JARVIS runtime, or AI auto-summaries of personal mail/calendar without an explicit user path designed for that purpose.
