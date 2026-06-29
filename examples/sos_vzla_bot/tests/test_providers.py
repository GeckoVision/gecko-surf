"""Provider pluggability — Anthropic ↔ OpenRouter (OpenAI-compatible).

The agent loop is written to the Anthropic Messages shape. ``OpenAICompatLLM``
adapts an OpenAI-compatible client (OpenRouter) to that shape, so ``agent.respond``
works unchanged against a free OpenRouter model. Tested offline with a fake OpenAI
client + recorded Gecko — no network, no keys.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from examples.sos_vzla_bot.agent import respond
from examples.sos_vzla_bot.providers import (
    OpenAICompatLLM,
    to_openai_messages,
    to_openai_tools,
)
from examples.sos_vzla_bot.surfcall_tools import SurfcallTools

SPEC = Path(__file__).resolve().parents[1] / "spec" / "sosvenezuela_openapi.json"


# --- fake OpenAI-compatible client (chat.completions.create) ---


def _msg(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls)


def _tool_call(id, name, arguments):
    return types.SimpleNamespace(
        id=id,
        type="function",
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def _resp(message, finish_reason):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)]
    )


class _Completions:
    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class FakeOpenAI:
    def __init__(self, script):
        self.chat = types.SimpleNamespace(completions=_Completions(script))


# --- translation ---


def test_to_openai_tools_shape():
    out = to_openai_tools(
        [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]
    )
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "f",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_to_openai_messages_translates_tool_turns():
    # An assistant tool_use block + a tool_result must become OpenAI tool_calls +
    # a role:"tool" message, with the system prompt prepended.
    class _B:
        def __init__(self, **k):
            self.__dict__.update(k)

    messages = [
        {"role": "user", "content": "hola"},
        {
            "role": "assistant",
            "content": [
                _B(type="tool_use", id="c1", name="searchPersons", input={"q": "Ana"})
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": '{"ok":1}'}
            ],
        },
    ]
    out = to_openai_messages("SYS", messages)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "hola"}
    assert out[2]["role"] == "assistant"
    tc = out[2]["tool_calls"][0]
    assert tc["id"] == "c1" and tc["function"]["name"] == "searchPersons"
    assert json.loads(tc["function"]["arguments"]) == {"q": "Ana"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok":1}'}


# --- adapter response shaping ---


def test_adapter_text_reply_maps_to_end_turn():
    llm = OpenAICompatLLM(FakeOpenAI([_resp(_msg(content="Hola 👋"), "stop")]))
    r = llm.messages.create(
        model="m",
        max_tokens=10,
        system="s",
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert r.stop_reason == "end_turn"
    assert [b.type for b in r.content] == ["text"]
    assert r.content[0].text == "Hola 👋"


def test_adapter_tool_call_maps_to_tool_use():
    llm = OpenAICompatLLM(
        FakeOpenAI(
            [_resp(_msg(tool_calls=[_tool_call("c1", "getNews", "{}")]), "tool_calls")]
        )
    )
    r = llm.messages.create(
        model="m",
        max_tokens=10,
        system="s",
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert r.stop_reason == "tool_use"
    block = r.content[0]
    assert block.type == "tool_use" and block.name == "getNews" and block.id == "c1"
    assert block.input == {}


# --- the real win: the agent loop runs unchanged over OpenRouter ---


def test_agent_loop_runs_through_openrouter_adapter():
    tools = SurfcallTools(SPEC, mode="recorded")
    fake = FakeOpenAI(
        [
            _resp(
                _msg(tool_calls=[_tool_call("c1", "searchPersons", '{"q":"Maria"}')]),
                "tool_calls",
            ),
            _resp(_msg(content="Encontré datos para Maria."), "stop"),
        ]
    )
    llm = OpenAICompatLLM(fake)
    out = respond("¿Maria?", llm=llm, tools=tools, model="free-model", system="s")
    assert out == "Encontré datos para Maria."
    # the second completion carried the recorded API result as a role:"tool" message
    tool_msgs = [
        m for m in fake.chat.completions.calls[1]["messages"] if m["role"] == "tool"
    ]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assert json.loads(tool_msgs[0]["content"])["status"] == 200
