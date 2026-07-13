"""The tool-use loop runs fully offline with a fake LLM + Gecko recorded mode."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from examples.txline_sharp_agent.agent import analyze
from examples.txline_sharp_agent.detector import SharpMove
from examples.txline_sharp_agent.surfcall_tools import TxlineTools

SPEC = str(
    Path(__file__).resolve().parents[2] / "txline_demo" / "spec" / "txline_openapi.yaml"
)

_MOVE = SharpMove(
    fixture_id=42,
    bookmaker="Pinnacle",
    market="1x2|",
    outcome="Home",
    old_pct=45.6,
    new_pct=54.8,
    delta=9.2,
    ts=1000,
)


class _FakeLLM:
    """Scripts: (1) call a tool, (2) return the analysis. Records what it was given."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_tool_names: list[str] = []
        self.messages = self  # so llm.messages.create works

    def create(self, **kwargs):
        self.seen_tool_names = [t["name"] for t in kwargs["tools"]]
        self.calls += 1
        if self.calls == 1:
            block = SimpleNamespace(
                type="tool_use",
                id="tu_1",
                name="getApiOddsSnapshotFixtureid",
                input={"fixtureId": 42},
            )
            return SimpleNamespace(stop_reason="tool_use", content=[block])
        text = SimpleNamespace(
            type="text", text="Home steamed +9.2pp — likely a lineup leak."
        )
        return SimpleNamespace(stop_reason="end_turn", content=[text])


def test_analyze_runs_tool_then_returns_text_offline():
    llm = _FakeLLM()
    tools = TxlineTools(SPEC)  # recorded + stub session
    out = analyze([_MOVE], llm=llm, tools=tools, model="fake")
    assert "Home steamed" in out
    assert llm.calls == 2  # tool round, then the answer
    # the model was only ever offered the allow-listed odds reads
    assert set(llm.seen_tool_names) <= tools.tool_names


def test_analyze_empty_moves_short_circuits():
    llm = _FakeLLM()
    tools = TxlineTools(SPEC)
    assert (
        analyze([], llm=llm, tools=tools, model="fake") == "No sharp move to analyze."
    )
    assert llm.calls == 0
