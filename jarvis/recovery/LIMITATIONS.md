# Agent Reliability & Recovery — Limitations (Phase 9.3)

- Interrupted workflows never resume mid-step automatically.
- Restart always creates a new plan/execution; prior approvals are void.
- Browser restart needs a live bridge session and fresh target scan when actions require elements.
- Goal write steps are not reconstructed from checkpoint content.
- Read-only retries are bounded and still need a new approval each time.
- Desktop Action Engine single-run sequences outside Multi-App are out of scope for this phase.
