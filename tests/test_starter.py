"""The generic forkable starter — build an app on ANY API with the SDK.

Proves the starter comprehends an arbitrary spec, picks the right capability for a
plain-language intent, and produces a well-formed (recorded) call — no LLM, no
network, no keys. The "here's a running app in ~20 lines on any API" path.
"""

from __future__ import annotations


# A throwaway spec for a totally different API than SOS — proves API-agnosticism.
SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/weather": {
            "get": {
                "operationId": "getWeather",
                "summary": "Current weather for a city",
                "parameters": [
                    {"name": "city", "in": "query", "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"temp": {"type": "number"}},
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}


def test_starter_finds_capability_and_makes_recorded_call() -> None:
    from examples._starter.app import run

    out = run(SPEC, "weather for a city", {"city": "Caracas"})

    assert out["chose"]["name"] == "getWeather"
    assert out["result"]["mode"] == "recorded"
    assert "temp" in out["result"]["data"]


def test_starter_parses_key_value_args() -> None:
    from examples._starter.app import _parse_kv

    assert _parse_kv(["city=Caracas", "days=3", "bad-no-eq"]) == {
        "city": "Caracas",
        "days": "3",
    }
