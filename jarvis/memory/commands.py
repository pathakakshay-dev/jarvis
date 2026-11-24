"""Explicit memory commands. Ordinary chat is never treated as consent to save."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_FACT_LENGTH = 280

_REMEMBER = re.compile(r"^(?:please\s+)?remember(?:\s+that)?\s*[:\-]?\s+(.+)$", re.IGNORECASE | re.DOTALL)
_REMEMBER_HI = re.compile(r"^yaad\s+rakh(?:na|o|lo)(?:\s+ki)?\s+(.+)$", re.IGNORECASE | re.DOTALL)
_LIST = re.compile(
    r"^(?:(?:list|show)(?:\s+my)?\s+memories|what do you remember|meri memories dikhao)\??$",
    re.IGNORECASE,
)
_SEARCH = re.compile(r"^(?:search memories(?:\s+for)?|memories about)\s+(.+)$", re.IGNORECASE | re.DOTALL)
_UPDATE = re.compile(
    r"^(?:update|correct|edit)\s+memory\s+(\d+)\s*[:\-]\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_DELETE = re.compile(r"^(?:delete|forget)\s+memory\s+(\d+)\??$", re.IGNORECASE)
_CLEAR = re.compile(r"^(?:clear|forget)\s+all\s+memories\??$", re.IGNORECASE)
_SENSITIVE = re.compile(
    r"(?i)\b(password|passcode|api[_ ]?key|secret|token|bearer|private key|ssh key)\b|\b\d{13,19}\b"
)
_WORD = re.compile(r"[A-Za-z0-9\u0900-\u097F]{3,}")
_STOP = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "have", "what", "when", "your",
        "you", "are", "was", "how", "can", "about", "please", "remember", "memory", "memories",
        "hai", "kya", "ki", "mera", "meri", "mein", "yaad", "rakhna", "rakho",
    }
)


@dataclass(frozen=True)
class MemoryCommand:
    kind: str
    fact: str = ""
    query: str = ""
    memory_id: int | None = None


def parse_memory_command(text: str) -> MemoryCommand | None:
    stripped = text.strip()
    update = _UPDATE.match(stripped)
    if update:
        return MemoryCommand("update", fact=update.group(2).strip(), memory_id=int(update.group(1)))
    delete = _DELETE.match(stripped)
    if delete:
        return MemoryCommand("delete_request", memory_id=int(delete.group(1)))
    if _CLEAR.match(stripped):
        return MemoryCommand("clear_request")
    search = _SEARCH.match(stripped)
    if search:
        return MemoryCommand("search", query=search.group(1).strip())
    if _LIST.match(stripped):
        return MemoryCommand("list")
    remember = _REMEMBER.match(stripped) or _REMEMBER_HI.match(stripped)
    if remember:
        return MemoryCommand("remember", fact=remember.group(1).strip())
    return None


def normalize_fact(text: str) -> str:
    return " ".join(text.split())


def validate_fact(text: str) -> tuple[str | None, str]:
    fact = normalize_fact(text)
    if not fact:
        return None, "A memory needs a short fact."
    if len(fact) > MAX_FACT_LENGTH:
        return None, f"That fact is too long. Keep it under {MAX_FACT_LENGTH} characters."
    if _SENSITIVE.search(fact):
        return None, "That looks like a secret. It was not saved."
    return fact, ""


def keywords(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _WORD.findall(text.lower()):
        if match in _STOP or match in seen:
            continue
        seen.add(match)
        found.append(match)
        if len(found) == 8:
            break
    return found


def fts_query(text: str) -> str:
    tokens = keywords(text)
    if not tokens:
        return ""
    return " OR ".join('"' + token.replace('"', "") + '"' for token in tokens)
