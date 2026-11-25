# JARVIS

Local text assistant for this Windows laptop, through **Phase 9.3**. It runs on `127.0.0.1`, keeps provider calls replaceable, and gates sensitive work behind permissions, per-action approval, and Emergency Stop.

This README describes what the current code actually does. It does not invent future phases.

## Capabilities (through Phase 9.3)

| Area | What it does |
|------|----------------|
| Chat | Local UI chat via Fake / Groq / Gemini providers |
| Memory | Explicit, controlled facts in SQLite (not auto-saved from replies) |
| Voice | Push-to-talk microphone; local Whisper transcription; optional Edge TTS |
| Web research | Opt-in search + bounded public HTTPS page reads |
| File skills | Workspace-jailed read / list / search / write / create folder |
| System & apps | System information and installed-application inventory |
| Clipboard | One-shot text clipboard read after approval |
| Desktop | Planner, mock simulator, restricted Notepad/Calculator open, focused desktop control, window focus switch, Desktop Action Engine |
| Goal Agent | Plans up to six existing skills; Start is separate from each step approval |
| Browser | Window intelligence, native-messaging bridge, page reader, single actions, short workflows |
| Multi-app | Orchestrates up to six steps across browser, desktop engine, Notepad, Calculator, system info |
| Google | Read-only Gmail + Calendar via official OAuth (Windows DPAPI token storage) |
| Reminders | Local task/reminder engine with notifications only (no shell/browser/desktop actions) |
| Recovery | Durable checkpoints for Goal, Multi-App, and Browser workflows; user-initiated abandon/restart only |

## Run

Python 3.12-compatible syntax. On this project machine the virtual environment may use a newer local Python if 3.12 is not installed.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m jarvis
```

Open http://127.0.0.1:8765

Default provider is `fake` (no API key, no network). For Groq or Gemini, set `JARVIS_PROVIDER` and the matching key in `.env`. Google integrations need `JARVIS_GOOGLE_CLIENT_ID` / `JARVIS_GOOGLE_CLIENT_SECRET` and a localhost redirect URI (see `.env.example`). Never commit `.env`.

Unpaid Gemini requests may be used to improve Google products. The page says so when Gemini is selected.

## Safety (summary)

- **Local auth:** API calls require the per-process UI token; Host/Origin are restricted to localhost.
- **Permissions:** Skill and feature flags default OFF and must be enabled in the UI.
- **Approvals:** Sensitive actions need an explicit approve/deny; plan Start does not approve individual steps.
- **Emergency Stop:** Halts work immediately; Resume does not replay dropped messages, approvals, or workflows.
- **Recovery:** Interrupted Goal / Multi-App / Browser workflows are marked for review and are **never** auto-resumed. Restart from beginning creates a **new** execution and does not reuse old approvals.
- **Privacy:** Secrets-like text is rejected or redacted from memory/audit where enforced; Google tokens stay in DPAPI-protected local storage under `data/` (not committed).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the capability map and [docs/CONTRIBUTING_CLOUD.md](docs/CONTRIBUTING_CLOUD.md) for Local / Cloud Agent collaboration rules.

## Memory

Saved facts are separate from chat. JARVIS stores one only from the memory panel or an explicit command such as `Remember that I prefer short answers.` Normal replies are not saved. Delete and Clear all require confirmation. Password/API-key shaped text is rejected.

## Voice

Hold **Hold to talk**. The microphone is off until that press. Transcription uses Whisper small on this computer. **Play** speaks the latest reply via Microsoft Edge TTS (reply text only) or falls back to local Windows voices. Mute defaults on. Diagnostic compare-decoding stores one clip under `data/diagnostic` only after agreement and deletes it afterward.

## Web research

Press **Research**, or start a message with `research:`. A normal **Send** does not search. `tavily` needs no key; `brave` uses `JARVIS_BRAVE_API_KEY`. JARVIS reads up to three public HTTPS pages and treats page text as data. Emergency Stop cancels research; Resume does not rerun it.

## Files, system, apps, clipboard

Workspace skills stay inside the approved workspace root. Each sensitive skill needs its permission enabled and its own approval where required. System info, app inventory, and clipboard read are separate skills with their own permissions.

## Desktop and Goal Agent

Desktop features are restricted (Notepad/Calculator launch, focused control, window switch, action engine). The desktop simulator is mock-only. The Goal Agent proposes a short plan of existing skills; reviewing/starting the plan does **not** approve each skill action.

## Browser

Browser Intelligence lists Edge/Chrome windows for optional title inspection. The Browser Bridge pairs a manually installed extension over native messaging. Page Reader snapshots approved text locally. Browser Actions and Browser Workflow propose short, approved click/focus/type/select-all/observe steps; action execution still uses the extension claim model. No arbitrary navigation, form submit, or credential flows.

## Multi-app, Google, reminders, recovery

- **Multi-app:** User proposes ≤6 steps; Start is separate; each underlying executor keeps its own permission and approvals.
- **Google:** Official OAuth, read-only Gmail/Calendar scopes only; no send/modify; no automatic AI forwarding of personal data.
- **Reminders:** Create/edit/cancel with validation; scheduler only notifies locally.
- **Recovery:** Workflow Recovery UI lists interrupted work for abandon or restart-from-beginning.

## Stop

- Emergency Stop is always on the page. The banner says Stopped only after the app confirms the halt.
- Ctrl+Alt+Shift+J requests Emergency Stop; Ctrl+Alt+Shift+K ends the process (when hotkeys register).
- Resume clears the halt. It does not replay a dropped message, approval, or task.
- Closing the window or ending Python in Task Manager is the fallback when the process is frozen.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

Use FakeProvider and mocked environments for automation. Live desktop, browser, and Google OAuth/DPAPI checks belong on the local Windows machine.

## Local data (do not commit)

Keep these on the machine only: `.env`, `data/` (SQLite, audit log, Whisper cache, encrypted Google tokens, checkpoints), `workspace/`, `.venv/`.
