"""System prompt assembly from the session context supplied by the gateway."""
from __future__ import annotations

import json

from .business import SessionContext

VOICE_RULES = """You are a phone agent speaking out loud on a live call.
Rules:
- Reply in 1-2 short spoken sentences. No lists, markdown, emojis or URLs.
- Reply in the caller's language. If the caller speaks Urdu, reply in Urdu written in Urdu (Nastaliq/Arabic) script, never in Roman Urdu or Hindi script. If the caller mixes Urdu and English, reply in Urdu script and keep common English words (order, booking, delivery) as the caller used them.
- Write numbers, prices and times the way they should be spoken.
- Use the provided tools for any order, booking, customer or handoff action. Never invent order numbers, prices, availability or policies; if a tool fails, say so briefly and offer a human.
- Ask for one piece of information at a time and confirm important details back to the caller.
- If the caller asks for a human, is upset, or the request is outside your instructions, use the handoff tool."""


def build_system_prompt(ctx: SessionContext) -> str:
    parts = [VOICE_RULES, f"\nBusiness: {ctx.business_name}", f"Business instructions:\n{ctx.agent_instructions.strip()}"]
    if ctx.knowledge:
        parts.append("Business knowledge:\n" + "\n".join(f"- {k}" for k in ctx.knowledge))
    if ctx.customer:
        parts.append("Known caller (from the business system):\n" + json.dumps(ctx.customer, ensure_ascii=False))
    return "\n\n".join(parts)


LANGUAGE_HINT = {
    "en": "The caller is speaking English: reply in English.",
    "ur": "The caller is speaking Urdu: reply in Urdu, written in Urdu script.",
}


def with_language_hint(messages: list[dict], language: str | None) -> list[dict]:
    """Copy of `messages` whose LAST user message is prefixed with the reply-language instruction.

    Small models drift into the language that dominates the history (first Colab test: English
    questions got Urdu answers). The hint is per request only; stored history stays clean.
    """
    hint = LANGUAGE_HINT.get(language or "")
    if not hint:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            out = list(messages)
            out[i] = {**messages[i], "content": f"[{hint}]\n{messages[i]['content']}"}
            return out
    return messages


def trim_history(messages: list[dict], char_budget: int) -> list[dict]:
    """Keep the system message plus the most recent turns within a character budget.

    Never splits an assistant tool_call from its tool results.
    """
    if not messages:
        return messages
    system, rest = messages[0], messages[1:]
    kept: list[dict] = []
    used = 0
    i = len(rest) - 1
    while i >= 0:
        # group trailing tool messages with the assistant message that requested them
        j = i
        while j >= 0 and rest[j].get("role") == "tool":
            j -= 1
        group = rest[j:i + 1] if j >= 0 else rest[: i + 1]
        size = sum(len(json.dumps(m, ensure_ascii=False)) for m in group)
        if kept and used + size > char_budget:
            break
        kept = group + kept
        used += size
        i = j - 1
    # history must not start with an orphan tool/assistant-tool message
    while kept and (kept[0].get("role") == "tool" or kept[0].get("tool_calls")):
        kept.pop(0)
    return [system] + kept
