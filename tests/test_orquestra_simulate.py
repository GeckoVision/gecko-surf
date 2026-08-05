"""Task 4 (Path A): the generic `simulate` tool on OrquestraProgramSurface.

Offline: assert the tool is LISTED with the right schema and that
``call_tool("simulate", ...)`` routes into the Receipt engine (the engine itself is
proven in test_simulate.py). We inject by monkeypatching the module-level
``gecko.providers.orquestra.simulate`` — no network, no build.
"""

from __future__ import annotations

from typing import Any

import gecko.providers.orquestra as orq
from gecko.providers.pumpfun import build_pumpfun_surface
from gecko.simulate import Receipt

RPC = "http://127.0.0.1:8899"
FEE_RECIPIENT = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"


def _plan() -> dict[str, Any]:
    return {
        "instruction": "buy",
        "accounts": {"user": FEE_RECIPIENT},
        "args": {"amount": 1},
        "feePayer": FEE_RECIPIENT,
        "build_url": "https://api.orquestra.dev/api/x/instructions/buy/build",
    }


def test_pumpfun_surface_lists_simulate_tool() -> None:
    surface = build_pumpfun_surface()
    names = {t["name"] for t in surface.list_tools()}
    assert names == {
        "get_program_graph",
        "derive_pda",
        "plan_buy",
        "plan_sell",
        "simulate",
    }


def test_simulate_tool_schema() -> None:
    surface = build_pumpfun_surface()
    tool = next(t for t in surface.list_tools() if t["name"] == "simulate")
    schema = tool["inputSchema"]
    assert schema["required"] == ["plan", "rpc_url"]
    assert set(schema["properties"]) == {
        "plan",
        "rpc_url",
        "fee_recipient",
        "track",
        "record_to",
    }
    # honesty is in the description the agent reads
    assert "never signs" in tool["description"]
    # the corpus opt-in is explicit: recording only happens when the agent asks
    assert "record_to" not in schema["required"]
    assert "opt in" in tool["description"]


def test_call_simulate_routes_to_engine_and_merges_fee_recipient(
    monkeypatch: object,
) -> None:
    surface = build_pumpfun_surface()
    captured: dict[str, Any] = {}

    def fake_simulate(plan: Any, **kwargs: Any) -> Receipt:
        captured["plan"] = plan
        captured["kwargs"] = kwargs
        return Receipt(
            status="pass",
            err=None,
            revert_class=None,
            units_consumed=31000,
            sol_delta=-100,
            tokens_received=None,
            logs_tail=("Program success",),
            network_label="simulated (fork/RPC snapshot — not mainnet)",
        )

    monkeypatch.setattr(orq, "simulate", fake_simulate)  # type: ignore[attr-defined]
    out = surface.call_tool(
        "simulate",
        {
            "plan": _plan(),
            "rpc_url": RPC,
            "fee_recipient": FEE_RECIPIENT,
            "track": [FEE_RECIPIENT],
        },
    )
    # the honest gap gets merged into accounts before the engine sees the plan
    assert captured["plan"]["accounts"]["fee_recipient"] == FEE_RECIPIENT
    assert captured["kwargs"]["rpc_url"] == RPC
    assert captured["kwargs"]["track"] == [FEE_RECIPIENT]
    # the tool returns the Receipt as a dict + the honesty note
    assert out["instruction"] == "buy"
    assert out["receipt"]["status"] == "pass"
    assert out["receipt"]["units_consumed"] == 31000
    assert "not mainnet" in out["note"].lower()


def test_call_simulate_requires_plan_and_rpc_url() -> None:
    surface = build_pumpfun_surface()
    assert "error" in surface.call_tool("simulate", {"rpc_url": RPC})
    assert "error" in surface.call_tool("simulate", {"plan": _plan()})


def _fake_receipt() -> Receipt:
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=31000,
        sol_delta=-100,
        tokens_received=None,
        logs_tail=("Program success",),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
    )


def test_call_simulate_with_record_to_appends_one_row(
    monkeypatch: object, tmp_path: object
) -> None:
    # The D2 opt-in on the Path-A tool: an explicit record_to argument appends ONE
    # categorical row to the path's simulated.jsonl sibling. Default (no argument)
    # stays record-nothing (proven by the routing test above writing no file).
    import json
    from pathlib import Path

    surface = build_pumpfun_surface()
    monkeypatch.setattr(orq, "simulate", lambda plan, **kw: _fake_receipt())  # type: ignore[attr-defined]
    corpus = Path(str(tmp_path)) / "corpus.jsonl"
    out = surface.call_tool(
        "simulate",
        {
            "plan": _plan(),
            "rpc_url": RPC,
            "fee_recipient": FEE_RECIPIENT,
            "record_to": str(corpus),
        },
    )
    assert out["receipt"]["status"] == "pass"
    assert "record_error" not in out
    sibling = corpus.with_name("simulated.jsonl")
    rows = [json.loads(line) for line in sibling.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["program_id"] == surface.program_id
    assert row["instruction"] == "buy"
    assert row["source"] == "simulated"
    # the merged fee_recipient pubkey (a resolved VALUE) never reaches the row
    assert FEE_RECIPIENT not in sibling.read_text()


def test_call_simulate_record_to_surfaces_a_control_plane_violation(
    monkeypatch: object, tmp_path: object
) -> None:
    # A plan whose accounts carry a resolved pubkey as a NAME is a control-plane
    # violation: the Receipt still returns, but record_error surfaces — and nothing
    # is written (fail closed, never swallowed).
    from pathlib import Path

    surface = build_pumpfun_surface()
    monkeypatch.setattr(orq, "simulate", lambda plan, **kw: _fake_receipt())  # type: ignore[attr-defined]
    corpus = Path(str(tmp_path)) / "corpus.jsonl"
    poisoned = _plan()
    poisoned["accounts"] = {FEE_RECIPIENT: FEE_RECIPIENT}  # pubkey posing as a name
    out = surface.call_tool(
        "simulate",
        {"plan": poisoned, "rpc_url": RPC, "record_to": str(corpus)},
    )
    assert out["receipt"]["status"] == "pass"  # the sim result is not discarded
    assert "record_error" in out
    assert FEE_RECIPIENT not in out["record_error"]  # redacted
    assert not corpus.with_name("simulated.jsonl").exists()
