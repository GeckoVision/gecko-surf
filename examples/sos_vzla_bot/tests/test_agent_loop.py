"""The Claude tool-use loop, exercised offline with a fake LLM + recorded gecko.

No network, no Anthropic, no Telegram — Pattern B. The fake LLM is scripted to
drive the manual agentic loop (Anthropic Messages shape: content blocks with
`type`/`id`/`name`/`input`/`text` + `stop_reason`), and the real Gecko engine
executes the tool in recorded mode.
"""

from __future__ import annotations

import json
from pathlib import Path

from examples.sos_vzla_bot.agent import FALLBACK_ES, respond
from examples.sos_vzla_bot.surfcall_tools import SurfcallTools

SPEC = Path(__file__).resolve().parents[1] / "spec" / "sosvenezuela_openapi.json"


class _Block:
    def __init__(self, type, *, text=None, id=None, name=None, input=None):
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = input


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


class FakeLLM:
    """Mimics the minimal `anthropic.Anthropic` surface the loop touches."""

    def __init__(self, script):
        self.messages = _Messages(script)


def _tools():
    return SurfcallTools(SPEC, mode="recorded")


def _tool_results(call):
    return [
        blk
        for m in call["messages"]
        if isinstance(m["content"], list)
        for blk in m["content"]
        if isinstance(blk, dict) and blk.get("type") == "tool_result"
    ]


def test_agent_executes_tool_then_replies_in_spanish():
    llm = FakeLLM(
        [
            _Resp(
                [
                    _Block(
                        "tool_use", id="t1", name="searchPersons", input={"q": "Maria"}
                    )
                ],
                "tool_use",
            ),
            _Resp(
                [_Block("text", text="Encontré coincidencias para Maria.")], "end_turn"
            ),
        ]
    )
    out = respond("¿está Maria Pérez?", llm=llm, tools=_tools(), model="m", system="s")
    assert out == "Encontré coincidencias para Maria."
    # The second call carried a tool_result for t1 — the loop fed the real
    # (recorded) API outcome back to the model.
    results = _tool_results(llm.messages.calls[1])
    assert results and results[0]["tool_use_id"] == "t1"
    assert json.loads(results[0]["content"])["status"] == 200


def test_loop_is_bounded_and_falls_back():
    # A model that never stops calling tools must not loop forever.
    never_stops = [
        _Resp([_Block("tool_use", id=f"t{i}", name="getNews", input={})], "tool_use")
        for i in range(10)
    ]
    llm = FakeLLM(never_stops)
    out = respond(
        "noticias", llm=llm, tools=_tools(), model="m", system="s", max_iters=3
    )
    assert out == FALLBACK_ES
    assert len(llm.messages.calls) == 3  # bounded


def test_disallowed_tool_call_is_handled_not_crashed():
    llm = FakeLLM(
        [
            _Resp(
                [_Block("tool_use", id="t1", name="deleteEverything", input={})],
                "tool_use",
            ),
            _Resp([_Block("text", text="No puedo hacer eso.")], "end_turn"),
        ]
    )
    out = respond("borra todo", llm=llm, tools=_tools(), model="m", system="s")
    assert out == "No puedo hacer eso."
    # the allow-list rejection was fed back as a structured error, no exception
    assert "error" in json.loads(_tool_results(llm.messages.calls[1])[0]["content"])


def test_empty_model_reply_falls_back():
    llm = FakeLLM([_Resp([], "end_turn")])
    out = respond("hola", llm=llm, tools=_tools(), model="m", system="s")
    assert out == FALLBACK_ES
