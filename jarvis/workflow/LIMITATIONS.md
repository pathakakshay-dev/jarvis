# Multi-App Workflow — Limitations (Phase 9.0)

- Maximum six steps. Supported capabilities only: system_info, open_notepad, open_calculator, desktop_engine, browser_workflow.
- Plans are user-structured in the JARVIS UI; this phase does not call an AI planner or Goal Agent.
- Each capability still needs its own permission enabled; multi-app permission does not grant them.
- Desktop Action Engine and Browser Workflow steps carry one nested action each; they are not a license to invent new actions.
- After approving a browser action step, the user must click **Run approved action** in the extension when required.
- Failed, mismatched, denied, cancelled, or expired steps stop the workflow with no automatic retry, replan, or replay.
- Resume after Emergency Stop does not restart or continue a multi-app workflow.
- Typed text and page/desktop snapshots are not persisted by the multi-app module to SQLite, memory, or audit detail.
