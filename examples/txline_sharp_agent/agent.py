"""The Claude tool-use loop — a flagged sharp move in, a trading read out.

This is the "agent that USES the API through Gecko" — the thesis embodied for a trading
tool. A manual agentic loop (Anthropic Messages API): hand the model the flagged move + the
allow-listed TxLINE odds tools; while it wants a tool, execute it through the ``TxlineTools``
seam and feed the result back; stop on ``end_turn``.

``llm`` is injected (the real ``anthropic.Anthropic`` client OR a fake), so the whole loop is
testable offline with Gecko's recorded mode — no network, no spend, no Anthropic import here.
Bounded by ``max_iters`` so a misbehaving model can never loop forever.
"""

from __future__ import annotations

from typing import Any

from .detector import SharpMove
from .surfcall_tools import TxlineTools

SYSTEM = (
    "You are a sharp-movement analyst for football (soccer) betting markets. You are given "
    "one or more flagged shifts in a fixture's implied probabilities (from the TxLINE feed). "
    "Use the odds tools to check the current snapshot if useful, then reply in 2-3 sentences: "
    "what moved, how significant it is, and the single most likely read (steam/injury/lineup/"
    "in-play event). Be concrete and terse. Never invent numbers the tools didn't return."
)

FALLBACK = (
    "Could not complete the analysis right now — the flagged move stands on its own."
)


def _text_of(resp: Any) -> str:
    parts = [
        getattr(b, "text", "") or ""
        for b in resp.content
        if getattr(b, "type", None) == "text"
    ]
    return "".join(parts).strip()


def _prompt_for(moves: list[SharpMove]) -> str:
    lines = ["Flagged sharp move(s):"] + [f"- {m.summary()}" for m in moves]
    lines.append("\nAnalyze and give the trading read.")
    return "\n".join(lines)


def analyze(
    moves: list[SharpMove],
    *,
    llm: Any,
    tools: TxlineTools,
    model: str,
    max_tokens: int = 512,
    max_iters: int = 4,
) -> str:
    """Run the tool-use loop over one batch of flagged moves; return the analyst's read."""
    if not moves:
        return "No sharp move to analyze."
    messages: list[dict[str, Any]] = [{"role": "user", "content": _prompt_for(moves)}]
    tool_defs = tools.tools_for_llm()

    for _ in range(max_iters):
        resp = llm.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM,
            tools=tool_defs,
            messages=messages,
        )
        if getattr(resp, "stop_reason", None) != "tool_use":
            return _text_of(resp) or FALLBACK

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

    return FALLBACK  # loop budget exhausted — degrade gracefully, never hang
