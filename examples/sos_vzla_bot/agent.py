"""The Claude tool-use loop — a Telegram message in, a Spanish reply out.

This is the "agent that USES the API through surfcall" — the thesis embodied. It
is a manual agentic loop (per the Anthropic Messages API): send the message + the
allow-listed surfcall tools; while the model wants a tool, execute it through the
``SurfcallTools`` seam and feed the result back; stop on ``end_turn``.

``llm`` is injected (the real ``anthropic.Anthropic`` client OR a fake), so the
whole loop is testable offline with surfcall's recorded mode — no network, no
spend, no Anthropic import here. Bounded by ``max_iters`` so a misbehaving model
can never loop forever.
"""

from __future__ import annotations

from typing import Any

from .surfcall_tools import SurfcallTools

FALLBACK_ES = (
    "Disculpa, no pude completar la consulta en este momento. Intenta de nuevo "
    "o revisa https://sosvenezuela2026.com. Para emergencias en Venezuela: 171."
)


def _text_of(resp: Any) -> str:
    parts = [
        getattr(b, "text", "") or ""
        for b in resp.content
        if getattr(b, "type", None) == "text"
    ]
    return "".join(parts).strip()


def respond(
    user_text: str,
    *,
    llm: Any,
    tools: SurfcallTools,
    model: str,
    system: str,
    history: list[dict[str, Any]] | None = None,
    max_tokens: int = 1024,
    max_iters: int = 4,
) -> str:
    """Run the tool-use loop for one user message; return the model's Spanish reply."""
    messages: list[dict[str, Any]] = list(history or []) + [
        {"role": "user", "content": user_text}
    ]
    tool_defs = tools.tools_for_llm()

    for _ in range(max_iters):
        resp = llm.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tool_defs,
            messages=messages,
        )
        if getattr(resp, "stop_reason", None) != "tool_use":
            return _text_of(resp) or FALLBACK_ES

        messages.append({"role": "assistant", "content": resp.content})
        results: list[dict[str, Any]] = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tools.call(block.name, block.input),
                    }
                )
        messages.append({"role": "user", "content": results})

    return FALLBACK_ES  # loop budget exhausted — degrade gracefully, never hang
