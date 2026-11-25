# Browser Workflow — Limitations (Phase 8.9)

- Maximum five steps. Supported steps only: observe, click, focus, type, select-all.
- Plans are user-structured in the JARVIS UI; this phase does not call an AI planner.
- Non-observe steps require Browser Actions permission and a fresh extension target scan.
- After approving an action step, the user must click **Run approved action** in the extension.
- Failed, mismatched, denied, cancelled, or expired steps stop the workflow with no automatic retry or replan.
- Resume after Emergency Stop does not restart or continue a workflow.
- Typed text and page snapshots are not persisted to SQLite, memory, or audit detail.
