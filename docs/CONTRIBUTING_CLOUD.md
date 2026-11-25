# Contributing with Local Agent and Cloud Agent

Rules for collaborating on [pathakakshay-dev/jarvis](https://github.com/pathakakshay-dev/jarvis) without weakening JARVIS safety controls or leaking local secrets.

## Roles

| Role | Owns |
|------|------|
| **Cloud Agent** | Isolated feature/docs branches, Pull Requests, mocked tests, design proposals that preserve safety boundaries |
| **Local Agent** | Integration on the Windows machine, live desktop/browser checks, Google OAuth/DPAPI validation, final review, and merge to `main` |

Cloud Agents must assume they cannot see or use the developer’s real `.env`, `data/`, or `workspace/`.

## Branch and Pull Request rules

1. Create an **isolated branch** for every change (for example `docs/…`, `feat/…`, `fix/…`).
2. Open a **Pull Request** into `main`.
3. **No direct pushes to `main`.**
4. Do not force-push shared branches unless the Local Agent explicitly requests it.
5. Keep PRs focused; do not mix unrelated refactors with safety-sensitive changes.

## Files Cloud Agents must never commit

- `.env` or any real API keys / OAuth client secrets
- `data/` (SQLite DBs, `audit.jsonl`, Whisper cache, diagnostic audio, workflow checkpoints, `*.tokens.dpapi`)
- `workspace/` (personal files)
- `.venv/` and local virtualenvs
- Tokens, credential JSON (`client_secret*.json`, `credentials.json`, `token.json`), private keys, logs copied from a live machine

Use `.env.example` for empty placeholders only. Prefer fixtures and temporary pytest dirs for tests.

## Safety boundaries Cloud Agents must not weaken

Do not change behavior in ways that reduce or bypass:

- Local authentication (API token, Host/Origin localhost checks)
- Permission defaults (features OFF until enabled)
- Per-action approval requirements
- Emergency Stop / HaltController cancellation semantics
- Audit logging of security-relevant outcomes
- Recovery rules: **no automatic resume**, no reuse of old approvals, fail-closed corrupt checkpoints
- Workspace path jail and shell-skill denials
- Browser/desktop restriction sets (no adding unrestricted navigation, form submit, credential capture, or arbitrary OS control “for convenience”)

If a task seems to require weakening a boundary, stop and document the conflict in the PR instead of shipping a bypass.

## What Cloud Agents should do

- Read `README.md` and `docs/ARCHITECTURE.md` before proposing changes.
- Prefer FakeProvider and mocked desktop/browser/Google adapters in automated tests.
- Add or update tests that prove approvals, stop, and recovery still fail closed.
- Keep documentation accurate; do not invent capabilities.
- Call out Windows-only or live-hardware follow-up needed from the Local Agent.

## What Local Agent owns before merge

- Run the full test suite on the Windows host when relevant.
- Live Notepad/Calculator/desktop/browser bridge checks when the PR touches those paths.
- Google OAuth connect/disconnect and DPAPI token storage checks when integrations change.
- Confirm `.gitignore` still excludes local secrets and runtime artifacts.
- Perform the final merge to `main` (Cloud Agents open the PR only).

## Suggested PR checklist

- [ ] Branch is isolated; targeting `main`
- [ ] No `.env`, `data/`, `workspace/`, tokens, or credentials in the diff
- [ ] Authentication / approval / stop / recovery controls unchanged or explicitly strengthened
- [ ] Tests added or updated with mocks only where required
- [ ] Docs updated if user-visible capability or safety behavior changed
- [ ] Local Agent follow-ups listed (OAuth, DPAPI, live desktop/browser) when applicable

## Documentation-only changes

Docs PRs (like this cloud-readiness map) must not modify application code or tests. Validation is accuracy against the existing tree, not a redesign of runtime behavior.
