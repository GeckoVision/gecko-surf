"""Rendering an engine answer for a chat window — the honesty rules, not the prose.

Three properties, each of which has a way of quietly breaking:

* a message never exceeds Telegram's 4096-character wire limit, and when it is cut the
  reader is told;
* an UNMEASURED fact is absent, never rendered as a measured zero;
* an empty menu with a filter and an empty menu without one are different sentences,
  because they are different facts.
"""

from __future__ import annotations

from typing import Any

from gecko.telegram_reply import (
    MAX_MESSAGE_CHARS,
    browse_text,
    clip,
    error_text,
    purchase_text,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_clip_marks_what_it_cut() -> None:
    out = clip("x" * (MAX_MESSAGE_CHARS * 2))
    assert len(out) <= MAX_MESSAGE_CHARS
    assert out.endswith("(cut short)")


def test_clip_leaves_a_short_message_alone() -> None:
    assert clip("hello") == "hello"


def test_an_empty_menu_says_which_kind_of_empty_it_is() -> None:
    nothing: dict[str, Any] = {"stores": []}
    unfiltered = browse_text(nothing, filtered=False)
    filtered = browse_text(nothing, filtered=True)
    assert "No storefronts" in unfiltered
    assert "Nothing matched that" in filtered
    assert unfiltered != filtered


def test_a_transport_error_in_the_menu_reads_as_our_failure() -> None:
    out = browse_text(
        {
            "error": {
                "code": "store-read-failed",
                "message": "reading failed",
                "hint": "retry once",
            }
        },
        filtered=False,
    )
    assert "failed on my side" in out
    assert "retry once" in out


def test_a_menu_with_many_stores_stays_inside_one_message() -> None:
    stores = [
        {
            "store": f"store{index}",
            "fulfilment": {"set": True},
            "products": [
                {
                    "name": f"thing{n}",
                    "price_ui": "1.5",
                    "mint": USDC,
                    "mint_note": "USDC",
                }
                for n in range(20)
            ],
        }
        for index in range(40)
    ]
    out = browse_text({"stores": stores}, filtered=False)
    assert len(out) <= MAX_MESSAGE_CHARS
    assert "more stores" in out


def test_undecodable_bytes_get_no_summary_at_all() -> None:
    # A sentence about bytes we could not read would be a guess wearing a decoder's
    # authority. The reply must say so instead of describing a movement.
    out = purchase_text(
        {
            "transaction": {
                "unsigned_transaction": "AAAA",
                "who_signs": "you do",
            }
        }
    )
    assert "could not decode" in out
    assert "Do not sign them on my word" in out


def test_unmeasured_facts_are_named_not_zeroed() -> None:
    out = purchase_text(
        {
            "effects": {
                "fee_payer": "x",
                "programs": [],
                "origin": "asserted",
                "unmeasured": ["token balances"],
            },
            "transaction": {"unsigned_transaction": "AAAA", "who_signs": "you do"},
        }
    )
    assert "not measured: token balances" in out


def test_a_missing_transaction_is_called_a_failure_not_a_purchase() -> None:
    out = purchase_text({"effects": {"fee_payer": "x", "programs": [], "origin": "a"}})
    assert "nothing to sign" in out
    assert "failure on my side" in out


def test_error_text_carries_only_a_class() -> None:
    out = error_text("RpcError")
    assert "RpcError" in out
    assert "not a refusal" in out
