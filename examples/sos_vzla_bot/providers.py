"""LLM provider pluggability — Anthropic ↔ OpenRouter, one switch.

``agent.respond`` is written to the Anthropic Messages shape (``llm.messages.create``
→ ``.content`` blocks + ``.stop_reason``). Anthropic's SDK already has that shape.
``OpenAICompatLLM`` gives the *same* shape over any OpenAI-compatible client
(OpenRouter), translating tool-calling both ways — so a **free** OpenRouter model
drives the exact same agent loop, with zero changes to ``agent.py``.

Why free-by-default but Haiku one env var away: free models support tool-calling
but are less reliable at first-call-correct tool use (the bot's whole job) and have
tighter rate limits. ``SOSBOT_PROVIDER=anthropic`` flips to Haiku 4.5 (~$0.005/chat).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Free, multilingual (incl. Spanish), reliable tool-calling — the zero-cost default.
DEFAULT_FREE_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


@dataclass
class _Block:
    """An Anthropic-shaped content block (what the agent loop reads)."""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


@dataclass
class _Resp:
    content: list[_Block] = field(default_factory=list)
    stop_reason: str = "end_turn"


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read a field whether the block is a dict (tool_result) or an object (_Block)."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def to_openai_messages(
    system: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Translate the agent loop's Anthropic-shaped messages to OpenAI chat messages."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(
                _get(b, "text") or "" for b in content if _get(b, "type") == "text"
            )
            tool_calls = [
                {
                    "id": _get(b, "id"),
                    "type": "function",
                    "function": {
                        "name": _get(b, "name"),
                        "arguments": json.dumps(_get(b, "input") or {}),
                    },
                }
                for b in content
                if _get(b, "type") == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        else:  # user turn carrying tool_result blocks
            for b in content:
                if _get(b, "type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": _get(b, "tool_use_id"),
                            "content": _get(b, "content") or "",
                        }
                    )
                else:
                    out.append({"role": "user", "content": _get(b, "text") or ""})
    return out


class _Adapter:
    def __init__(self, client: Any) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> _Resp:
        resp = self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=to_openai_messages(system, messages),
            tools=to_openai_tools(tools) or None,
        )
        msg = resp.choices[0].message
        blocks: list[_Block] = []
        if getattr(msg, "content", None):
            blocks.append(_Block("text", text=msg.content))
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append(
                _Block("tool_use", id=tc.id, name=tc.function.name, input=args)
            )
        return _Resp(blocks, "tool_use" if tool_calls else "end_turn")


class OpenAICompatLLM:
    """Quacks like ``anthropic.Anthropic`` for the agent loop, backed by an
    OpenAI-compatible client (e.g. OpenRouter)."""

    def __init__(self, client: Any) -> None:
        self.messages = _Adapter(client)


def openrouter_llm(
    api_key: str, *, base_url: str = OPENROUTER_BASE_URL
) -> OpenAICompatLLM:
    from openai import OpenAI  # optional dep (sosbot extra)

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={"X-Title": "SOS Venezuela 2026 bot (surfcall)"},
    )
    return OpenAICompatLLM(client)


def make_llm(provider: str, api_key: str) -> Any:
    """Build the LLM client for the agent loop, by provider."""
    if provider == "openrouter":
        return openrouter_llm(api_key)
    import anthropic  # optional dep (sosbot extra)

    return anthropic.Anthropic(api_key=api_key)
