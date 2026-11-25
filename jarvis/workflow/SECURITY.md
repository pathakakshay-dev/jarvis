# Multi-App Workflow Orchestrator — Security Review (Phase 9.0)

## Architecture
The Multi-App Orchestrator is a dedicated module. It does not execute desktop or browser actions itself.

1. User proposes a structured plan (≤6 steps) naming supported capabilities only.
2. User explicitly Starts the plan after reviewing affected apps.
3. Each step arms the existing executor (Browser Workflow, Desktop Action Engine, Notepad, Calculator, or System Info).
4. Nested plan Start (browser/desktop) is a multi-app checkpoint; per-action approvals use the underlying executor’s own `action_id` and are forwarded with the user’s decision.
5. Executors retain their own permission flags, validation, revalidation, and verification.
6. Browser action steps that require the extension claim stay in a waiting/running state until a status poll observes completion — there is no background worker.

No Goal Agent, AI planner, unrestricted Skill Orchestrator tasking, Desktop Control, simulator, shell, or new desktop/browser actions.

## Permissions
Multi-app permission is independent and OFF by default. Underlying permissions remain independent and are rechecked by those modules.

## Controls reviewed
- Plan validation rejects unsupported capabilities and enforces the 6-step cap
- Start is separate from step approvals
- Nested confirmations do not auto-approve underlying actions
- Approval in one module does not grant permission in another
- Executor calls run outside the multi-app lock to avoid lock-order deadlocks
- Emergency Stop clears multi-app expect state; Resume does not replay
- Typed text is not written into multi-app audit detail
- No provider access to desktop/browser contents from this module

## Findings
1. **Accepted:** Browser click/focus/type/select-all steps still require the user to run the approved action from the extension.
2. **Accepted:** Desktop non-observe steps still require a prior Desktop Action Engine observation (same as using the engine alone).
3. **Accepted:** Nested browser/desktop Start is approved once at the multi-app checkpoint; the underlying Start API still revalidates permission and generation, and per-action approvals remain separate.
4. **No safety boundary was relaxed** to ship this phase.

## Out of scope
Arbitrary skills, shell/PowerShell, unrestricted launching, coordinates, file deletion, credentials, background automation, browser navigation, form submit, and AI replanning.
