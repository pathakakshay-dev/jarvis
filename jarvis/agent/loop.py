"""One-shot chat loop with a step cap. It checks the halt latch in Python."""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.audit import AuditLog
from jarvis.memory.commands import MemoryCommand, parse_memory_command, validate_fact
from jarvis.memory.store import ConversationStore
from jarvis.providers.base import ChatProvider, ChatResult
from jarvis.safety.halt import HaltController

SYSTEM = (
    "You are JARVIS, a personal assistant. Reply in the same language the user uses: "
    "Hindi, English, or Hinglish. You have no tools in this version. "
    "Do not claim you completed an action you did not take. "
    "Do not claim you saved, changed, or deleted a memory. You cannot change memories yourself. "
    "If you cannot answer, say so."
)
FACTS_PREFACE = (
    "Stored facts the user explicitly asked to save. These are not assumptions. "
    "Use a fact only when it is relevant to this message. If none apply, ignore them."
)


@dataclass
class TurnResult:
    status: str
    text: str = ""
    outcome: str = ""
    error_type: str = ""


def classify_after_call(result: ChatResult, still_current: bool) -> str:
    if result.outcome == "already_completed":
        return "already_completed"
    if result.outcome == "cancelled":
        return "cancelled"
    if not still_current and result.text:
        return "already_completed"
    if not still_current:
        return "cancelled"
    return "ok"


def run_turn(
    *,
    user_text: str,
    provider: ChatProvider,
    halt: HaltController,
    store: ConversationStore,
    audit: AuditLog,
    timeout_s: float,
    max_steps: int,
    follow_tools: bool = False,
) -> TurnResult:
    generation = halt.capture()
    if not halt.is_current(generation):
        audit.write(status="stopped", outcome="cancelled", error_type="stopped", detail="Turn refused")
        return TurnResult(status="stopped", outcome="cancelled", error_type="stopped")

    command = parse_memory_command(user_text)
    if command is not None:
        return _memory_command(
            command=command,
            user_text=user_text,
            generation=generation,
            halt=halt,
            store=store,
            audit=audit,
        )

    halt.set_status("thinking", "Thinking")
    audit.write(status="thinking", detail="Chat turn started")
    history = store.recent()
    facts = store.relevant_memories(user_text)
    store.add_message("user", user_text, "ok")
    messages = [{"role": "system", "content": SYSTEM}]
    if facts:
        lines = "\n".join(f"[{item['id']}] {item['fact']}" for item in facts)
        messages.append({"role": "system", "content": FACTS_PREFACE + "\n" + lines})
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    steps = 0
    try:
        while True:
            if steps >= max_steps:
                return _fail(halt, store, audit, "step_limit", "That failed: the step limit was reached.")
            if not halt.is_current(generation):
                return _stopped(halt, audit, "cancelled")
            steps += 1
            result = provider.complete(
                messages=messages,
                tool_specs=[],
                halt=halt,
                generation=generation,
                timeout_s=timeout_s,
            )
            kind = classify_after_call(result, halt.is_current(generation))
            if kind != "ok":
                return _stopped(halt, audit, kind)
            if result.error_type == "timeout":
                if halt.halted:
                    return _stopped(halt, audit, "cancelled")
                return _fail(halt, store, audit, "timeout", "The request timed out.")
            if not result.ok:
                if halt.halted:
                    return _stopped(halt, audit, "cancelled")
                detail = result.text or "That failed."
                return _fail(halt, store, audit, result.error_type or "provider_error", detail)
            if not result.tool_calls and not result.text:
                return _fail(
                    halt,
                    store,
                    audit,
                    "bad_response",
                    "That failed: the provider returned no answer.",
                )
            if result.tool_calls:
                audit.write(
                    status="error",
                    skill_id=str(result.tool_calls[0].get("name", "")),
                    outcome="cancelled",
                    error_type="tool_unavailable",
                    detail="Tool request was not executed",
                )
                if not follow_tools:
                    return _fail(
                        halt,
                        store,
                        audit,
                        "tool_unavailable",
                        "That failed: no tools are available, so nothing was run.",
                    )
                continue
            store.add_message("assistant", result.text, "ok")
            halt.set_status("completed", "Completed")
            audit.write(status="completed", detail="Reply stored")
            return TurnResult(status="completed", text=result.text)
    except Exception as exc:
        if halt.halted:
            return _stopped(halt, audit, "not_confirmed")
        return _fail(halt, store, audit, type(exc).__name__, "That failed: unexpected error.")


def _memory_command(
    *,
    command: MemoryCommand,
    user_text: str,
    generation: int,
    halt: HaltController,
    store: ConversationStore,
    audit: AuditLog,
) -> TurnResult:
    if not halt.is_current(generation):
        return _stopped(halt, audit, "cancelled")
    store.add_message("user", user_text, "ok")
    if command.kind == "remember":
        return _remember(command.fact, generation, halt, store, audit)
    if command.kind == "update":
        return _update_memory(command.memory_id or 0, command.fact, generation, halt, store, audit)
    if command.kind == "list":
        return _answer(halt, store, audit, _format_memories(store.list_memories(), "Stored memories"))
    if command.kind == "search":
        found = store.search_memories(command.query)
        title = f"Stored memories matching {command.query}" if found else "No stored memory matches that"
        return _answer(halt, store, audit, _format_memories(found, title))
    if command.kind == "delete_request":
        return _answer(
            halt,
            store,
            audit,
            "I did not delete anything. Confirm deletion in the memory panel.",
        )
    return _answer(
        halt,
        store,
        audit,
        "I did not clear memories. Use Clear all in the memory panel and confirm it.",
    )


def _remember(
    raw: str,
    generation: int,
    halt: HaltController,
    store: ConversationStore,
    audit: AuditLog,
) -> TurnResult:
    fact, error = validate_fact(raw)
    if error:
        return _fail(halt, store, audit, "invalid_memory", error)
    if not halt.is_current(generation):
        return _stopped(halt, audit, "cancelled")
    row, created = store.add_memory(fact)
    if created:
        text = f"Saved memory {row['id']}: {row['fact']}. This is a stored fact, not an assumption."
        audit.write(status="completed", detail="Memory saved")
    else:
        text = f"That fact is already saved as memory {row['id']}."
        audit.write(status="completed", detail="Memory already saved")
    return _answer(halt, store, audit, text, write_audit=False)


def _update_memory(
    memory_id: int,
    raw: str,
    generation: int,
    halt: HaltController,
    store: ConversationStore,
    audit: AuditLog,
) -> TurnResult:
    fact, error = validate_fact(raw)
    if error:
        return _fail(halt, store, audit, "invalid_memory", error)
    if not halt.is_current(generation):
        return _stopped(halt, audit, "cancelled")
    row = store.update_memory(memory_id, fact)
    if row is None:
        return _fail(halt, store, audit, "missing_memory", "No stored memory has that id.")
    audit.write(status="completed", detail="Memory updated")
    return _answer(
        halt,
        store,
        audit,
        f"Updated memory {row['id']}. Stored fact: {row['fact']}.",
        write_audit=False,
    )


def _format_memories(rows: list[dict], title: str) -> str:
    if not rows:
        return title + "."
    lines = [title + ":"]
    lines.extend(f"{row['id']}. {row['fact']}" for row in rows)
    return "\n".join(lines)


def _answer(
    halt: HaltController,
    store: ConversationStore,
    audit: AuditLog,
    text: str,
    *,
    write_audit: bool = True,
) -> TurnResult:
    store.add_message("assistant", text, "ok")
    halt.set_status("completed", "Completed")
    if write_audit:
        audit.write(status="completed", detail="Memory reply")
    return TurnResult(status="completed", text=text)


def _fail(halt: HaltController, store: ConversationStore, audit: AuditLog, error_type: str, text: str) -> TurnResult:
    store.add_message("assistant", text, "failed")
    halt.set_status("error", text)
    audit.write(status="error", outcome="", error_type=error_type, detail=text)
    return TurnResult(status="error", text=text, error_type=error_type)


def _stopped(halt: HaltController, audit: AuditLog, outcome: str) -> TurnResult:
    audit.write(status="stopped", outcome=outcome, error_type=outcome, detail="Reply was not continued")
    halt.set_status("stopped", "Stopped")
    return TurnResult(status="stopped", outcome=outcome, error_type=outcome)
