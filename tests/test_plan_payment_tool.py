"""`plan_payment` on the catalog surface: wired IS NOT reached, so drive `call_tool`."""

import pytest

from gecko.providers.catalog_surface import OrquestraCatalogSurface


def _surface() -> OrquestraCatalogSurface:
    return OrquestraCatalogSurface()


def test_the_tool_is_listed() -> None:
    names = [t["name"] for t in _surface().list_tools()]
    assert "plan_payment" in names


def test_the_free_browse_ordering_is_preserved() -> None:
    """The existing invariant must survive the insertion."""
    names = [t["name"] for t in _surface().list_tools()]
    assert names.index("try_purchase") == names.index("prepare_purchase") + 1


def test_the_schema_declares_what_the_answer_actually_needs() -> None:
    tool = next(t for t in _surface().list_tools() if t["name"] == "plan_payment")
    props = tool["inputSchema"]["properties"]
    # the held mint is the caller's WALLET, which is why this needs a buyer at all
    assert {"store", "product", "buyer", "network"} <= set(props)
    assert set(tool["inputSchema"]["required"]) == {"store", "product", "buyer"}


def test_the_description_does_not_promise_the_evidence_never_expires() -> None:
    """`list_stores` may say 'expires never' — it reads a menu. This wraps a POINT-IN-TIME
    peg verdict, and the conversion happens later in the caller's own wallet."""
    tool = next(t for t in _surface().list_tools() if t["name"] == "plan_payment")
    assert "expires never" not in tool["description"]
    assert "peg_evidence_as_of" in tool["description"]


def test_the_surface_routes_the_tool() -> None:
    out = _surface().call_tool("plan_payment", {})
    assert "unknown tool" not in str(out)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"store": "geckocoffee"},
        {"store": "geckocoffee", "product": "Espresso"},
        {"store": "", "product": "x", "buyer": "y"},
    ],
)
def test_a_missing_argument_is_a_refusal_not_a_crash(args: dict) -> None:
    out = _surface().call_tool("plan_payment", args)
    assert "error" in out


def test_an_rpc_url_without_a_network_is_refused() -> None:
    out = _surface().call_tool(
        "plan_payment",
        {
            "store": "geckocoffee",
            "product": "Espresso",
            "buyer": "5cjBs5VE8WVVctG2EoUkYiRkW92sXkoT4YsNxszWC9CE",
            "rpc_url": "http://127.0.0.1:8899",
        },
    )
    assert "error" in out
    assert "network" in out["error"].lower()


def test_a_malformed_buyer_never_reaches_the_network() -> None:
    calls = []

    def rpc_call(url, method, params):
        calls.append(method)
        return {}

    surface = OrquestraCatalogSurface(purchase_rpc_call=rpc_call)
    out = surface.call_tool(
        "plan_payment",
        {"store": "geckocoffee", "product": "Espresso", "buyer": "not a pubkey"},
    )
    assert "error" in out
    assert calls == []
