"""#75 — canonical example synthesis from a DECLARED value-domain entity.

`verify-docs --live` synthesizes an arg for every path/query param. When the spec ships
NO ``example`` on a path param, today's fallback is a type placeholder (``"sample"``) that
404s a real endpoint → the op is falsely REFUTED. Slice-1 fixed this for specs that SHIP
an example; this closes the gap for specs that ship NONE but DECLARE the param's value
domain (an x-gecko / customer-confirmed entity): synthesis fills a real, stable canonical
for that KNOWN domain instead of a placeholder.

Honesty is law (Pattern B — every test is offline, $0, no network):
  * ONLY a KNOWN value domain with a registered canonical is filled. An unknown/undeclared
    domain keeps the placeholder — the verdict stays honestly UNVERIFIED/REFUTED, never a
    fabricated VERIFIED.
  * Precedence: shipped ``example`` > canonical-from-declared-entity > placeholder.

The flagship proof is Pegana ``by-mint`` (ships NO example on ``{mint}``): with the mint
value domain DECLARED it goes UNVERIFIED(placeholder-404) → VERIFIED(canonical-mint-200),
proven with an injected fake transport that answers 200 iff the canonical mint is in the URL.
(The un-declared placeholder-404 case is UNVERIFIED, never REFUTED — a made-up argument is
not evidence that the endpoint is missing; see ``tests/test_verify_false_refute.py``.)
"""

from __future__ import annotations

from pathlib import Path

from gecko import verify
from gecko.access import NoAuthSession
from gecko.caller import PreparedRequest
from gecko.canonical import USDC_MINT, canonical_example
from gecko.client import AgentApiClient
from gecko.sample import example_from_schema
from gecko.validator import example_args

_FIXTURES = Path(__file__).parent / "fixtures"
_PEGANA = _FIXTURES / "pegana_p0_openapi.json"
_MINT_HINT = {"mint": "solana-token-mint"}
_BY_MINT_OPS = ("detail_by_mint", "state_by_mint", "peg_feed_by_mint")


# --- the registry ----------------------------------------------------------------------
def test_registry_resolves_solana_token_mint_to_the_canonical_usdc_mint() -> None:
    # keyed on the normalized entity form — every spelling of the value domain resolves.
    for spelling in ("solana-token-mint", "solanatokenmint", "SOLANA_TOKEN_MINT"):
        assert canonical_example(spelling) == USDC_MINT


def test_registry_is_honest_for_an_unknown_or_absent_domain() -> None:
    # no fabrication: an unregistered domain and a missing entity both yield NO canonical.
    assert canonical_example("solana-pool") is None
    assert canonical_example("") is None
    assert canonical_example(None) is None


# --- example synthesis at the schema level ---------------------------------------------
def test_declared_entity_with_no_example_synthesizes_the_canonical() -> None:
    schema = {"type": "string", "x-gecko-entity": "solana-token-mint"}
    assert example_from_schema(schema) == USDC_MINT


def test_no_declared_entity_keeps_the_placeholder() -> None:
    # the honest fallback: a plain string with no declared domain is still the placeholder.
    assert example_from_schema({"type": "string"}) == "sample"


def test_unregistered_declared_entity_keeps_the_placeholder() -> None:
    # a DECLARED but UNKNOWN value domain has no canonical → placeholder, never a guess.
    assert example_from_schema({"type": "string", "x-gecko-entity": "solana-pool"}) == (
        "sample"
    )


def test_shipped_example_wins_over_the_canonical() -> None:
    # precedence: a spec-shipped example beats the canonical (slice-1 stays on top).
    schema = {
        "type": "string",
        "example": "So11111111111111111111111111111111111111112",
        "x-gecko-entity": "solana-token-mint",
    }
    assert example_from_schema(schema) == "So11111111111111111111111111111111111111112"


# --- tool build: the marker is stamped only for a KNOWN, declared domain ----------------
def test_declared_mint_param_stamps_the_marker_and_fills_the_canonical() -> None:
    client = AgentApiClient(
        str(_PEGANA),
        base_url="https://api.pegana.xyz",
        session=NoAuthSession(),
        declared_hints=_MINT_HINT,
    )
    tool = client._tool_by_name["detail_by_mint"]
    mint_schema = tool["inputSchema"]["properties"]["mint"]
    assert mint_schema.get("x-gecko-entity") == "solana-token-mint"
    assert example_args(tool) == {"mint": USDC_MINT}


def test_without_a_declared_hint_the_mint_param_stays_a_placeholder() -> None:
    # no injected hint and the spec ships no example → today's honest placeholder.
    client = AgentApiClient(
        str(_PEGANA), base_url="https://api.pegana.xyz", session=NoAuthSession()
    )
    tool = client._tool_by_name["detail_by_mint"]
    assert "x-gecko-entity" not in tool["inputSchema"]["properties"]["mint"]
    assert example_args(tool) == {"mint": "sample"}


# --- the flagship: Pegana by-mint verify before/after (injected transport, Pattern B) ---
def _mint_gated_transport() -> "object":
    """A fake upstream that serves 200 iff the canonical mint lands in the URL, else 404 —
    exactly how the real endpoint behaves (a placeholder mint is not a real asset)."""

    def transport(req: PreparedRequest) -> tuple[int, object]:
        return (200, {"ok": True}) if USDC_MINT in req.url else (404, {"error": "nf"})

    return transport


def test_pegana_by_mint_verifies_when_the_mint_domain_is_declared() -> None:
    client = AgentApiClient(
        str(_PEGANA),
        base_url="https://api.pegana.xyz",
        session=NoAuthSession(),
        declared_hints=_MINT_HINT,
        live_transport=_mint_gated_transport(),
    )
    result = verify.verify_docs(client, mode="live")
    for op_id in _BY_MINT_OPS:
        assert result["report"][op_id]["status"] == "VERIFIED"
        assert result["report"][op_id]["basis"] == ["replay:200"]


def test_pegana_by_mint_is_unverified_not_refuted_without_the_declaration() -> None:
    """Without the declaration the placeholder still 404s — but that is a missing ASSET,
    not a missing ENDPOINT, so the honest verdict is UNVERIFIED naming the param we could
    not ground.

    This test previously asserted REFUTED (the falsely-accusing behaviour it was written to
    document). That verdict published "this documented endpoint does not exist" about an
    endpoint that answers 200 with a real mint — see ``tests/test_verify_false_refute.py``
    for the full rule. The declaration now upgrades UNVERIFIED → VERIFIED rather than
    REFUTED → VERIFIED."""
    client = AgentApiClient(
        str(_PEGANA),
        base_url="https://api.pegana.xyz",
        session=NoAuthSession(),
        live_transport=_mint_gated_transport(),
    )
    result = verify.verify_docs(client, mode="live")
    for op_id in _BY_MINT_OPS:
        assert result["report"][op_id]["status"] == "UNVERIFIED"
        assert result["report"][op_id]["basis"] == [
            "replay:404",
            "no-real-argument:mint",
        ]
