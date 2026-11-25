# Agent Reliability & Recovery — Security Review (Phase 9.3)

## Architecture
Durable checkpoints for Goal Agent, Multi-App Workflow, and Browser Workflow.

1. Engines write privacy-minimal checkpoints on propose/start/progress/finish.
2. On process start, any `active` checkpoint is sealed as `interrupted` — never auto-resumed.
3. Recovery UI allows only **abandon** or **restart from beginning**.
4. Restart proposes a **new** execution from sanitized metadata; old approvals and action IDs are never reused.
5. Read-only Goal steps may re-queue for a fresh approval after timeout (bounded). Write/desktop/browser actions are never auto-retried.

## Safety checks
- Checkpoint rows validated before use; corrupt state fails closed
- Restart payloads omit secret-like text and never include approval IDs
- Stop/cancel persists terminal recovery status
- Concurrent abandon/restart invalidate live approvals first
- Existing per-action approval requirements unchanged

## Findings
1. **Accepted:** Browser/Multi-App restart still requires permissions, targets, and user Start/approvals.
2. **Accepted:** Write-file goal restarts re-plan from goal text only (content is not restored from the checkpoint).
3. **No safety boundary was relaxed.**
