"""The Capability Card and its admission check, falsified offline.

The card is what a contributor writes: the bindings and the sentences, everything else
derived. What this file pins is the part that decides whether a card is admitted, and each
case is one way a card can lie about the surface it describes:

* an UNDECLARED refusal — the card says an instruction works and it does not;
* a declared gap that no longer FIRES — the card describes a surface that has moved;
* a paraphrase that echoes the instruction name — measures keyword matching, not routing;
* a card promoting itself to `verified` — the one label a file may never claim;
* a card with no owner — Home Assistant's code owner, and the reason a dead card can be
  chased to a person.

Routing is reported and never rejects: a sentence our ranker misses is a fact about the
ranker. That asymmetry is asserted here so nobody "fixes" it later by failing the card.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.capability import (
    CapabilityCardError,
    check_card,
    load_card,
    render,
)

IDL = Path(__file__).parent / "fixtures" / "let_me_buy_idl.json"
PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"

BINDINGS = """
[bindings]
signer        = "GpaLFMwQWh2xuBkMQGKmcYT5A1WgYJekofu6DJjp8W9c"
authority     = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
mint          = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
store_name    = "geckocoffee"
product_name  = "Espresso"
name          = "Espresso"
price         = "100000"
table_number  = "1"
"""

GAPS = """
[[declared_gap]]
instruction = "mark_as_delivered"
refusal     = "MissingProbeBindingError"

[[declared_gap]]
instruction = "update_details"
refusal     = "MissingProbeBindingError"

[[declared_gap]]
instruction = "update_telegram_channel"
refusal     = "MissingProbeBindingError"
"""


def _card(
    tmp_path: Path,
    *,
    body: str = "",
    tier: str = "community",
    owner: str = "@student",
    bindings: str = BINDINGS,
    gaps: str = GAPS,
    paraphrase: str | None = None,
) -> Path:
    text = f"""
[surface]
slug   = "letmebuy"
kind   = "solana-program"
source = "{PROGRAM}"
idl    = "{IDL}"
owner  = "{owner}"
tier   = "{tier}"
{bindings}
[intents]
make_purchase = "get me an espresso and pay with my stablecoins"

{
        paraphrase
        if paraphrase is not None
        else '''[[paraphrase]]
intent  = "I am thirsty and I have a wallet"
expects = "make_purchase"'''
    }
{gaps}{body}
"""
    path = tmp_path / "card.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_honest_partial_is_admitted_and_counts_what_derives(tmp_path: Path) -> None:
    """Five derive, three are declared by refusal class. Demanding completeness would
    admit almost nothing, so a card that names its gaps is a good card."""
    report = check_card(load_card(_card(tmp_path)), environ={})
    assert report.admitted
    assert (report.derived, report.declared_gaps) == (5, 3)
    assert report.undeclared == ()


def test_an_undeclared_refusal_keeps_the_card_out(tmp_path: Path) -> None:
    report = check_card(load_card(_card(tmp_path, gaps="")), environ={})
    assert not report.admitted
    names = {one.instruction for one in report.undeclared}
    assert names == {"mark_as_delivered", "update_details", "update_telegram_channel"}
    assert all(one.refusal == "MissingProbeBindingError" for one in report.undeclared)


def test_a_declared_gap_that_no_longer_fires_also_keeps_it_out(tmp_path: Path) -> None:
    """The card is describing a surface it no longer matches, and that is a lie in the
    other direction. Same verdict, different reason, and the report says which."""
    extra = (
        GAPS
        + """
[[declared_gap]]
instruction = "make_purchase"
refusal     = "UnderivableAccountError"
"""
    )
    report = check_card(load_card(_card(tmp_path, gaps=extra)), environ={})
    assert not report.admitted
    assert report.phantom_gaps == ("make_purchase",)
    assert report.undeclared == ()


def test_a_missed_sentence_is_reported_and_never_rejects(tmp_path: Path) -> None:
    """Our ranker scores 0.00 on paraphrase intents today. A card that exposes that is
    evidence, and failing it would hide the only demand signal we get."""
    nonsense = """[[paraphrase]]
intent  = "zzz qqq vvv nothing on earth routes this"
expects = "make_purchase"
"""
    report = check_card(load_card(_card(tmp_path, paraphrase=nonsense)), environ={})
    assert report.admitted, "a routing miss is not a rejection"
    missed = [one for one in report.routings if not one.routed]
    assert any(one.paraphrase for one in missed)


def test_a_card_cannot_promote_itself(tmp_path: Path) -> None:
    with pytest.raises(CapabilityCardError, match="promoted by review"):
        load_card(_card(tmp_path, tier="verified"))


def test_a_card_names_a_human(tmp_path: Path) -> None:
    with pytest.raises(CapabilityCardError, match="names a human"):
        load_card(_card(tmp_path, owner="nobody"))


def test_a_paraphrase_that_echoes_the_instruction_name_is_refused(
    tmp_path: Path,
) -> None:
    echo = """[[paraphrase]]
intent  = "make the purchase please"
expects = "make_purchase"
"""
    with pytest.raises(CapabilityCardError, match="shares"):
        load_card(_card(tmp_path, paraphrase=echo))


def test_a_card_without_a_paraphrase_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CapabilityCardError, match="at least one"):
        load_card(_card(tmp_path, paraphrase=""))


def test_a_gap_must_name_a_refusal_class_we_raise(tmp_path: Path) -> None:
    invented = """
[[declared_gap]]
instruction = "mark_as_delivered"
refusal     = "ItDidNotWorkError"
"""
    with pytest.raises(CapabilityCardError, match="not one of"):
        load_card(_card(tmp_path, gaps=invented))


def test_the_check_refuses_to_run_beside_a_key(tmp_path: Path) -> None:
    """Nothing signs. The check does not even start next to a live signing key."""
    with pytest.raises(CapabilityCardError, match="runs beside no key"):
        check_card(load_card(_card(tmp_path)), environ={"PAYBOX_SIGNIN_KEY": "pbxk1.x"})


def test_a_solana_card_without_an_idl_errors_rather_than_reaching_the_chain(
    tmp_path: Path,
) -> None:
    path = tmp_path / "card.toml"
    path.write_text(
        f"""
[surface]
slug = "x"
kind = "solana-program"
source = "{PROGRAM}"
owner = "@student"
tier = "community"
[intents]
make_purchase = "buy me a coffee"
[[paraphrase]]
intent = "I am thirsty and I have a wallet"
expects = "make_purchase"
""",
        encoding="utf-8",
    )
    report = check_card(load_card(path), environ={})
    assert not report.admitted
    assert any("names its IDL" in message for message in report.errors)


def test_the_report_renders_as_json_and_as_markdown(tmp_path: Path) -> None:
    report = check_card(load_card(_card(tmp_path)), environ={})
    payload = json.loads(render(report, as_json=True))
    assert payload["admitted"] is True
    assert payload["instructions"]["derived"] == 5
    assert payload["owner"] == "@student"
    assert "5 of 8 instructions derive" in render(report)


def test_the_shipped_example_card_is_admitted() -> None:
    """The card we hand a student to read must itself pass, or it teaches the wrong thing."""
    example = Path(__file__).parent.parent / "capabilities" / "letmebuy.toml"
    report = check_card(load_card(example), environ={})
    assert report.admitted, render(report)
