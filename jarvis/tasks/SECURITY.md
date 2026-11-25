# Task & Reminder Engine — Security Review (Phase 9.2)

## Architecture
A dedicated engine with SQLite persistence and a lightweight tick scheduler.

1. Users create reminders through the API/UI with validated fields.
2. Edit and cancel require an explicit approval checkpoint.
3. The scheduler only calls a local notifier — it never runs shell, skills, browser, desktop, email, or calendar actions.
4. Delivery records use a primary key on `(reminder_id, occurrence)` to prevent duplicates across ticks, threads, and restarts.
5. Emergency Stop suspends delivery and marks due occurrences as missed; Resume does not replay notifications.

Isolated from Goal Agent, Multi-App Workflow, Browser Workflow, Desktop Agent, Gmail, and Calendar.

## Controls reviewed
- Title/notes reject secret-like content
- Recurrence limited to daily/weekly with a ≤365-day end bound
- Snooze limited to predefined durations
- Timezone validated via `zoneinfo`
- Past due times rejected on create
- Permission OFF by default
- Audit details stay privacy-minimal (no reminder body text beyond generic status)
- No automatic provider forwarding

## Findings
1. **Accepted:** Local notifications are in-app queue based in this phase (no OS toast / no PowerShell).
2. **Accepted:** Missed reminders are indicated, not silently executed after stop/restart.
3. **No safety boundary was relaxed.**

## Out of scope
Arbitrary scheduled code, shell tasks, automatic emails, browser/desktop automation, AI-generated tasks from personal data.
