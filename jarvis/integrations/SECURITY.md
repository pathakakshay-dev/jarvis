# Personal Integrations — Security Review (Phase 9.1)

## Architecture
Integrations are isolated from Goal Agent, Multi-App Orchestrator, Desktop/Browser agents, Memory, and chat providers.

1. Official Google OAuth 2.0 authorization-code flow with PKCE.
2. Redirect URI is localhost-only: `http://127.0.0.1:<port>/api/integrations/google/callback`.
3. Tokens are encrypted at rest with Windows DPAPI for the interactive user (`google.tokens.dpapi` under `data/integrations/`).
4. Gmail and Calendar adapters are separate; each has its own permission flag (OFF by default) and per-request approval.
5. API calls use HaltController guarded HTTP with timeouts; Emergency Stop cancels in-flight clients.

## OAuth scopes
Minimum read-only scopes only:

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar.readonly`

Token responses that include extra scopes beyond an allowlist are rejected. Modification scopes are never requested.

## Credential storage
- Client ID/secret: environment / local `.env` only (never committed).
- Access/refresh tokens: DPAPI-protected blob on disk; tests inject `MemoryProtector`.
- Disconnect clears the local blob and attempts Google token revocation.
- Authorization codes, tokens, and client secrets are never written to audit detail, chat, SQLite, or provider prompts.

## Controls reviewed
- OAuth `state` one-time validation; forged callbacks rejected
- Permission OFF blocks Gmail/Calendar requests
- Explicit user request + approval before each fetch
- Result size limits and local rate limits
- Malformed / oversized API payloads fail closed
- No background polling or automatic summaries
- No automatic provider forwarding of mail/calendar content

## Findings
1. **Accepted:** OAuth client secret for a local installed/web client still resides in `.env`; protect the machine account.
2. **Accepted:** DPAPI binds ciphertext to the Windows user profile; another OS user cannot decrypt it.
3. **Accepted:** Revocation is best-effort if Google is unreachable; local credentials are still deleted.
4. **No safety boundary was relaxed** to ship this phase.

## Out of scope
Send/reply/modify mail, create/edit calendar events, browser-automated Google login, background sync, AI auto-summaries of personal data.
