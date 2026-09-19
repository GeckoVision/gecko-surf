"""A human accepts ONE Token-2022 mint's extension set, by name, with its state pinned.

USDG (Paxos) carries a transfer hook, a permanent delegate, a zero transfer fee and
confidential transfer. Every one of those is in the simulation's unsound table, so the
spend gate could never measure a USDG leg and the mainnet route refused at leg 1 on
2026-09-18 (``amount-unresolvable``). The founder's ruling: the human names the mint and
pins its extension set and hook program in the policy; the simulation measures the leg
from balances only while the chain still reads exactly that; the gate checks the
acceptance the simulation applied against the one the policy carries.

What this file pins, and in which direction each failure falls:

* no acceptance → the refusal is unchanged (nothing here loosens the default);
* acceptance + matching reading → measured, and the acceptance is ON the report;
* acceptance + a reading that moved (an extension appeared, the hook re-pointed) →
  ``mint-extensions-changed``, never measured;
* acceptance + a bare list of names (the hook program unread) → refused;
* an acceptance may not name an unwaivable or unreviewed extension at all;
* the gate refuses a report measured under an acceptance the policy never authored, and
  one the policy authored in a different state, before any cap is compared.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.simulate import (
    POLICY_ACCEPTABLE_REFUSALS,
    TOKEN_2022_PROGRAM_ID,
    AcceptedMint,
    TokenDeltaReport,
    TokenDeltaUnmeasurable,
    parse_token_deltas,
)
from gecko.spend_policy import SpendPolicy
from gecko.token_program import MintExtensions, read_mint_extensions
from tests.test_simulate_token_delta import OWNER, _balance, _value
from tests.test_spend_policy import (
    PAYER,
    _gate,
    _measured,
    _movement,
    _policy,
    _receipt,
    _tx,
)

USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
#: USDG's extension set as the chain reported it on 2026-09-18 (``getAccountInfo``
#: jsonParsed): the hook is reserved but points nowhere.
USDG_NAMES = (
    "mintCloseAuthority",
    "permanentDelegate",
    "transferFeeConfig",
    "confidentialTransferMint",
    "confidentialTransferFeeConfig",
    "transferHook",
    "metadataPointer",
    "tokenMetadata",
)
USDG_READ = MintExtensions(names=USDG_NAMES, transfer_hook_program=None)
USDG_ACCEPTED = AcceptedMint(
    mint=USDG, extensions=frozenset(USDG_NAMES), transfer_hook_program=None
)
SOME_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"


def _usdg_value(*, pre: str = "1000000", post: str = "0") -> dict[str, Any]:
    return _value(
        [_balance(mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount=pre)],
        [_balance(mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount=post)],
    )


def _report(**kw: Any) -> TokenDeltaReport:
    report = parse_token_deltas(_usdg_value(), **kw)
    assert report is not None
    return report


# --- the simulation ----------------------------------------------------------------------


def test_without_an_acceptance_usdg_still_refuses_exactly_as_before() -> None:
    report = _report(mint_extensions={USDG: USDG_READ})
    assert report.status == "unmeasurable"
    assert report.acceptances == ()
    (refusal,) = report.refusals
    assert refusal.reason in POLICY_ACCEPTABLE_REFUSALS
    assert refusal.mint == USDG
    with pytest.raises(TokenDeltaUnmeasurable):
        report.outflows()


def test_an_acceptance_that_matches_the_reading_measures_and_is_recorded() -> None:
    report = _report(
        mint_extensions={USDG: USDG_READ}, accepted_mints={USDG: USDG_ACCEPTED}
    )
    assert report.status == "measured"
    assert report.acceptances == (USDG_ACCEPTED,)
    (outflow,) = report.outflows()
    assert (outflow.mint, outflow.owner, outflow.raw) == (USDG, OWNER, 1_000_000)


def test_an_acceptance_is_recorded_once_per_mint_across_many_accounts() -> None:
    value = _value(
        [
            _balance(index=3, mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount="5"),
            _balance(index=4, mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount="5"),
        ],
        [
            _balance(index=3, mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount="0"),
            _balance(index=4, mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount="0"),
        ],
    )
    report = parse_token_deltas(
        value, mint_extensions={USDG: USDG_READ}, accepted_mints={USDG: USDG_ACCEPTED}
    )
    assert report is not None
    assert report.acceptances == (USDG_ACCEPTED,)
    (outflow,) = report.outflows()
    assert outflow.raw == 10


@pytest.mark.parametrize(
    "reading",
    [
        # an extension APPEARED since the human looked
        MintExtensions(
            names=(*USDG_NAMES, "interestBearingConfig"), transfer_hook_program=None
        ),
        # an extension VANISHED
        MintExtensions(names=USDG_NAMES[1:], transfer_hook_program=None),
        # the hook was RE-POINTED at a program
        MintExtensions(names=USDG_NAMES, transfer_hook_program=SOME_PROGRAM),
    ],
    ids=["appeared", "vanished", "hook-repointed"],
)
def test_a_reading_that_moved_refuses_as_changed_never_measures(
    reading: MintExtensions,
) -> None:
    report = _report(
        mint_extensions={USDG: reading}, accepted_mints={USDG: USDG_ACCEPTED}
    )
    assert report.status == "unmeasurable"
    assert report.acceptances == ()
    (refusal,) = report.refusals
    assert refusal.reason == "mint-extensions-changed"
    assert refusal.mint == USDG
    assert "the state moved" in refusal.detail


def test_a_bare_list_of_names_cannot_satisfy_an_acceptance() -> None:
    """The original evidence shape carries no hook program, so a pinned hook cannot be
    checked against it; the acceptance is not applied and the mint refuses as before."""
    report = _report(
        mint_extensions={USDG: USDG_NAMES}, accepted_mints={USDG: USDG_ACCEPTED}
    )
    assert report.status == "unmeasurable"
    (refusal,) = report.refusals
    assert refusal.reason in POLICY_ACCEPTABLE_REFUSALS
    assert "full reading" in refusal.detail


def test_an_acceptance_for_one_mint_does_nothing_for_another() -> None:
    other = "So11111111111111111111111111111111111111112"
    value = _value(
        [_balance(mint=other, program=TOKEN_2022_PROGRAM_ID, amount="5")],
        [_balance(mint=other, program=TOKEN_2022_PROGRAM_ID, amount="0")],
    )
    hooked = MintExtensions(names=("transferHook",), transfer_hook_program=None)
    report = parse_token_deltas(
        value, mint_extensions={other: hooked}, accepted_mints={USDG: USDG_ACCEPTED}
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.refusals[0].reason == "transfer-hook"


def test_an_unread_mint_refuses_even_when_accepted() -> None:
    report = _report(mint_extensions=None, accepted_mints={USDG: USDG_ACCEPTED})
    assert report.refusals[0].reason == "token-2022-extensions-unread"


def test_an_unreviewed_extension_refuses_even_inside_an_accepted_set() -> None:
    """An acceptance waives what the tables call unsound; it cannot vouch for a name the
    tables have never seen. ``AcceptedMint`` will not even construct over one."""
    with pytest.raises(ValueError, match="outside both review tables"):
        AcceptedMint(
            mint=USDG,
            extensions=frozenset({"transferHook", "somethingNew"}),
            transfer_hook_program=None,
        )


@pytest.mark.parametrize(
    "name", ["interestBearingConfig", "scaledUiAmountConfig", "nonTransferable"]
)
def test_an_unwaivable_extension_cannot_be_accepted(name: str) -> None:
    with pytest.raises(ValueError, match="no acceptance may waive"):
        AcceptedMint(
            mint=USDG, extensions=frozenset({name}), transfer_hook_program=None
        )


def test_an_acceptance_normalises_names_so_spelling_is_not_a_second_state() -> None:
    a = AcceptedMint(
        mint=USDG,
        extensions=frozenset({"transfer_hook_account", "TransferHook"}),
        transfer_hook_program=None,
    )
    assert a.extensions == frozenset({"transferhookaccount", "transferhook"})
    assert a.matches(
        MintExtensions(
            names=("transferHook", "transferHookAccount"), transfer_hook_program=None
        )
    )


def test_a_pinned_hook_program_must_be_a_public_key() -> None:
    with pytest.raises(ValueError, match="base58"):
        AcceptedMint(
            mint=USDG,
            extensions=frozenset({"transferHook"}),
            transfer_hook_program="nope",
        )


# --- the gate ----------------------------------------------------------------------------


def _accepted_report(applied: AcceptedMint = USDG_ACCEPTED) -> TokenDeltaReport:
    return TokenDeltaReport(
        status="measured",
        movements=(_movement(mint=USDG, owner=str(PAYER), delta_raw=-1),),
        refusals=(),
        acceptances=(applied,),
    )


def test_the_gate_refuses_an_acceptance_the_policy_never_authored() -> None:
    verdict = _gate(_policy()).authorize(
        _tx(), _receipt(token_delta=_accepted_report()), now=1_000.0
    )
    assert not verdict.authorized
    assert verdict.code == "mint-acceptance-not-authored"


def test_the_gate_refuses_an_acceptance_the_policy_authored_in_another_state() -> None:
    stale = AcceptedMint(
        mint=USDG, extensions=frozenset(USDG_NAMES), transfer_hook_program=SOME_PROGRAM
    )
    verdict = _gate(_policy(accepted_mints=frozenset({stale}))).authorize(
        _tx(), _receipt(token_delta=_accepted_report()), now=1_000.0
    )
    assert not verdict.authorized
    assert verdict.code == "mint-acceptance-stale"


def test_the_gate_reads_the_amount_once_the_acceptance_matches_the_policy() -> None:
    """With the acceptance matched, the ordinary cap logic runs: USDG has no cap in this
    policy, so the next answer is ``mint-not-allowlisted`` — the leg was MEASURED and
    then judged, which is the whole point."""
    verdict = _gate(_policy(accepted_mints=frozenset({USDG_ACCEPTED}))).authorize(
        _tx(), _receipt(token_delta=_accepted_report()), now=1_000.0
    )
    assert not verdict.authorized
    assert verdict.code == "mint-not-allowlisted"


def test_an_acceptance_on_the_policy_changes_nothing_for_an_unaccepted_leg() -> None:
    verdict = _gate(_policy(accepted_mints=frozenset({USDG_ACCEPTED}))).authorize(
        _tx(), _receipt(token_delta=_measured(_movement(delta_raw=-1))), now=1_000.0
    )
    assert verdict.authorized


def test_a_policy_refuses_two_acceptances_for_one_mint() -> None:
    other = AcceptedMint(
        mint=USDG, extensions=frozenset({"transferHook"}), transfer_hook_program=None
    )
    with pytest.raises(ValueError, match="two acceptances"):
        SpendPolicy(accepted_mints=frozenset({USDG_ACCEPTED, other}))


def test_the_gate_refusal_codes_are_in_the_closed_vocabulary() -> None:
    from typing import get_args

    from gecko.spend_policy import SpendRefusalCode

    codes = set(get_args(SpendRefusalCode))
    assert {"mint-acceptance-not-authored", "mint-acceptance-stale"} <= codes


# --- the reader --------------------------------------------------------------------------


def _usdg_account(hook_program: Any = None, extensions: Any = None) -> dict[str, Any]:
    if extensions is None:
        extensions = [
            {"extension": "mintCloseAuthority", "state": {"closeAuthority": "x"}},
            {"extension": "permanentDelegate", "state": {"delegate": "y"}},
            {"extension": "transferFeeConfig", "state": {}},
            {"extension": "confidentialTransferMint", "state": {}},
            {"extension": "confidentialTransferFeeConfig", "state": {}},
            {
                "extension": "transferHook",
                "state": {"authority": "z", "programId": hook_program},
            },
            {"extension": "metadataPointer", "state": {}},
            {"extension": "tokenMetadata", "state": {}},
        ]
    return {
        "owner": TOKEN_2022_PROGRAM_ID,
        "data": {
            "program": "spl-token-2022",
            "parsed": {
                "type": "mint",
                "info": {"decimals": 6, "extensions": extensions},
            },
        },
    }


def _rpc(values: list[Any]):
    calls: list[tuple[str, list[Any]]] = []

    def call(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        calls.append((method, params))
        assert method == "getMultipleAccounts"
        assert params[1] == {"encoding": "jsonParsed"}
        return {"result": {"value": values}}

    call.calls = calls  # type: ignore[attr-defined]
    return call


def test_the_reader_returns_the_names_and_the_hook_program_the_node_reported() -> None:
    read = read_mint_extensions(
        [USDG], rpc_url="http://node", rpc_call=_rpc([_usdg_account()])
    )
    assert read == {USDG: USDG_READ}
    assert USDG_ACCEPTED.matches(read[USDG])
    read = read_mint_extensions(
        [USDG], rpc_url="http://node", rpc_call=_rpc([_usdg_account(SOME_PROGRAM)])
    )
    assert read[USDG].transfer_hook_program == SOME_PROGRAM
    assert not USDG_ACCEPTED.matches(read[USDG])


def test_the_reader_leaves_out_what_it_could_not_read_rather_than_defaulting() -> None:
    """Absent is what the parser refuses as unread; an empty set would read as classic."""
    assert (
        read_mint_extensions([USDG], rpc_url="http://node", rpc_call=_rpc([None])) == {}
    )
    not_a_mint = {
        "owner": TOKEN_2022_PROGRAM_ID,
        "data": {"parsed": {"type": "account", "info": {}}},
    }
    assert (
        read_mint_extensions([USDG], rpc_url="http://node", rpc_call=_rpc([not_a_mint]))
        == {}
    )
    holed = _usdg_account(extensions=[{"extension": "transferHook"}, {"nope": 1}])
    assert (
        read_mint_extensions([USDG], rpc_url="http://node", rpc_call=_rpc([holed]))
        == {}
    )

    def failing(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        raise OSError("down")

    assert read_mint_extensions([USDG], rpc_url="http://node", rpc_call=failing) == {}


def test_the_reader_reports_a_classic_mint_as_an_empty_set() -> None:
    classic = {
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}},
    }
    read = read_mint_extensions(
        ["EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"],
        rpc_url="http://node",
        rpc_call=_rpc([classic]),
    )
    assert read == {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": MintExtensions(
            names=(), transfer_hook_program=None
        )
    }


# --- the review's findings, each closed by a test (defi-security review, 2026-09-18) ----


def test_evidence_that_omits_the_unsound_extensions_is_refused_by_the_gate() -> None:
    """S1. Forging an acceptance is closed above; DOWNGRADING the evidence is the other
    door. Names that leave out the hook make USDG measure as sound with no acceptance
    applied, so the gate's cross-check would never run. The policy carries the human's
    word that the mint IS unsound, so a measured, unaccepted movement of it is refused."""
    downgraded = parse_token_deltas(_usdg_value(), mint_extensions={USDG: []})
    assert downgraded is not None
    assert downgraded.status == "measured" and downgraded.acceptances == ()
    report = TokenDeltaReport(
        status="measured",
        movements=(_movement(mint=USDG, owner=str(PAYER), delta_raw=-1),),
        refusals=(),
    )
    verdict = _gate(_policy(accepted_mints=frozenset({USDG_ACCEPTED}))).authorize(
        _tx(), _receipt(token_delta=report), now=1_000.0
    )
    assert not verdict.authorized
    assert verdict.code == "mint-acceptance-not-applied"


def test_a_policy_cannot_accept_a_mint_beside_a_token_program_instruction() -> None:
    """S2. Two of the four waivers rest on no token-program instruction being admitted
    directly; the policy refuses to be authored otherwise."""
    from gecko.simulate import TOKEN_2022_PROGRAM_ID as T22
    from gecko.spend_policy import AllowedInstruction

    with pytest.raises(ValueError, match="token-program instruction"):
        _policy(
            accepted_mints=frozenset({USDG_ACCEPTED}),
            allowed_instructions=frozenset(
                {AllowedInstruction(program_id=T22, discriminator=b"\x0c")}
            ),
        )


def test_the_route_passes_the_acceptance_to_the_convert_leg_only() -> None:
    """S3. The purchase leg's policy never authored the acceptance; handing it the
    convert leg's would refuse leg 2 after leg 1 had landed on mainnet."""
    from gecko import autonomous_purchase as ap
    from tests.test_autonomous_purchase_route import _prepared_purchase
    from tests.test_autonomous_purchase_relay import (
        BUYER,
        BuyerBackend,
        FakeRelay,
        _gate as _relay_gate,
        _relay_built_tx,
        _rpc as _relay_rpc,
        _signer,
    )

    seen: list[object] = []
    real = ap.settle_sponsored

    def spy(*args, **kwargs):
        seen.append(kwargs.get("accepted_mints"))
        return real(*args, **kwargs)

    ap.settle_sponsored = spy  # type: ignore[assignment]
    try:
        backend = BuyerBackend()
        ap.settle_route(
            _relay_built_tx().tx,
            prepare_purchase=_prepared_purchase,
            network="fork",
            rpc_url="http://127.0.0.1:8999",
            relay=FakeRelay(),
            convert_signer=_signer(_relay_gate(), backend),
            purchase_signer=_signer(_relay_gate(), backend),
            authority=BUYER,
            rpc_call=_relay_rpc(),
            convert_accepted_mints={USDG: USDG_ACCEPTED},
            sleep=lambda _s: None,
        )
    finally:
        ap.settle_sponsored = real  # type: ignore[assignment]
    assert seen == [{USDG: USDG_ACCEPTED}, None]


def test_an_unmeasurable_report_carries_no_acceptance() -> None:
    """S5. Otherwise the gate diagnoses the acceptance when the refusal beside it is the
    real problem."""
    other = "So11111111111111111111111111111111111111112"
    value = _value(
        [
            _balance(index=3, mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount="5"),
            _balance(index=4, mint=other, program=TOKEN_2022_PROGRAM_ID, amount="5"),
        ],
        [
            _balance(index=3, mint=USDG, program=TOKEN_2022_PROGRAM_ID, amount="0"),
            _balance(index=4, mint=other, program=TOKEN_2022_PROGRAM_ID, amount="0"),
        ],
    )
    report = parse_token_deltas(
        value, mint_extensions={USDG: USDG_READ}, accepted_mints={USDG: USDG_ACCEPTED}
    )
    assert report is not None
    assert report.status == "unmeasurable"
    assert report.refusals[0].reason == "token-2022-extensions-unread"
    assert report.acceptances == ()
    with pytest.raises(ValueError, match="applied no acceptance"):
        TokenDeltaReport(
            status="unmeasurable",
            movements=(),
            refusals=report.refusals,
            acceptances=(USDG_ACCEPTED,),
        )


def test_an_acceptance_over_non_string_names_raises_value_error() -> None:
    """S6. The documented contract is ValueError, not whatever ``.lower`` raises."""
    with pytest.raises(ValueError, match="set of names"):
        AcceptedMint(mint=USDG, extensions=frozenset({1}), transfer_hook_program=None)  # type: ignore[arg-type]
