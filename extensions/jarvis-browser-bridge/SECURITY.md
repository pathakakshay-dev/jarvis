# Browser Bridge / Reader / Actions / Workflow — Security Review (Phase 8.9)

## Architecture
Browser Workflow is a dedicated module. It does not talk to the page directly.

1. User proposes a structured plan (≤5 steps) from scanned Browser Action targets.
2. User explicitly Starts the plan.
3. Each step requires its own approval.
4. `observe` validates a fresh target scan.
5. `click` / `focus` / `type` / `select_all` are proposed through Browser Actions (`for_workflow=True`), then claim/result executes via the existing extension injectors.
6. The workflow advances only after a verified action result.

No Goal Agent, Desktop Action Engine, Desktop Control, simulator, or planner coupling. No provider-based planning and no automatic page-content forwarding.

## Permissions
Extension permissions unchanged:

- `nativeMessaging`
- `activeTab`
- `scripting`

Browser Workflow permission is independent and OFF by default. Browser Actions permission remains required for non-observe steps and is rechecked when arming those steps.

## Controls reviewed
- Plan validation rejects unsupported actions and enforces the 5-step cap
- Start is separate from step approvals
- Targets and fingerprints are revalidated before action steps
- No retries or autonomous replanning after failure/mismatch
- Emergency Stop clears pending workflow state; Resume does not replay
- Concurrent step approvals are serialized on the shared bridge lock
- Typed text stays out of audit detail and is not provider-forwarded by this module
- Existing bridge/reader/action boundaries were not relaxed

## Findings
1. **Accepted:** Workflow progress still depends on the user running approved actions from the extension (claim model).
2. **Accepted:** Observe confirms target-scan freshness, not a full page snapshot.
3. **No safety boundary was relaxed** to ship this phase.

## Out of scope
Arbitrary JS/selectors, navigation, form submit, credentials/payment flows, background monitoring, cross-app workflows, and LLM-driven page instruction following.
