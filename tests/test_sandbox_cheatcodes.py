"""The cheatcode wrappers, checked against a REAL fork — by effect, never by status.

Two kinds of test live here and the split is deliberate:

* ``fork``-marked legs start a local surfpool mainnet fork and assert the STATE that
  each cheatcode leaves behind — a balance read back, an ATA that did not exist and now
  does, a clock that landed where it was sent. A cheatcode that returns
  ``{"value": null}`` and changes nothing is the exact failure mode this module was
  written to make impossible, so "the call succeeded" is never the assertion.
  Deselect with ``-m "not fork"``.
* offline legs use a light fake transport to prove the wrappers cannot be aimed anywhere
  but the proven endpoint, and that the parameter shapes we send are the measured ones.

Nothing here signs, and nothing here reaches mainnet: the fork is a local validator, and
every offline leg is a dict.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from gecko.pda_testkit import SurfpoolError, SurfpoolFork, surfpool_status
from gecko.sandbox import (
    CheatcodeError,
    SurfnetProof,
    TimeTravelError,
    fund_sol,
    fund_token,
    prove_surfnet,
    reset_account,
    time_travel,
)

FORK_PORT = 8934
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

#: The measured surfnet-info body, so an offline proof can be built without a validator.
MEASURED_INFO_RESULT: dict[str, Any] = {
    "context": {"slot": 439792934, "apiVersion": "3.1.6"},
    "value": {"runbookExecutions": []},
}

SKIP_REASON = (
    "surfpool is not on PATH (or GECKO_FORK_DEMO=0) — THE FORK LEG DID NOT RUN and "
    "NOTHING below was measured against a validator: " + surfpool_status().detail
)
needs_fork = pytest.mark.skipif(
    not surfpool_status().available or os.getenv("GECKO_FORK_DEMO") == "0",
    reason=SKIP_REASON,
)


def offline_proof(rpc_url: str = "http://127.0.0.1:8899") -> SurfnetProof:
    """A proof built from the measured body — no validator, no key, no network."""
    return SurfnetProof(rpc_url=rpc_url, detail=MEASURED_INFO_RESULT)


def fresh_pubkey() -> str:
    """A pubkey nothing on mainnet has ever touched — the honest 'creates it?' subject."""
    from solders.keypair import Keypair

    return str(Keypair().pubkey())


@pytest.fixture(scope="module")
def fork() -> Any:
    """One fork for the whole module: starting it is the expensive part, not using it."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    try:
        with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=120) as running:
            yield running
    except SurfpoolError as exc:
        pytest.skip(f"THE FORK LEG DID NOT RUN — surfpool never became ready: {exc}")


@pytest.fixture(scope="module")
def proof(fork: Any) -> SurfnetProof:
    return prove_surfnet(fork.rpc_url)


# --- offline: the endpoint is the proof's, and the shape is the measured one ---


def test_every_cheatcode_goes_to_the_proven_endpoint_and_nowhere_else() -> None:
    """The binding, stated as a test: the URL is read off the proof, never passed in.

    A wrapper that accepted an ``rpc_url`` would let a caller prove one endpoint and
    mutate another. Recording the URL of every request is how that stays impossible.
    """
    seen: list[tuple[str, str, list[Any]]] = []

    def transport(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        seen.append((url, method, params))
        if method == "getBalance":
            return {"result": {"context": {"slot": 1}, "value": 7}}
        if method == "getAccountInfo":
            return {"result": {"context": {"slot": 1}, "value": None}}
        return {"result": {"context": {"slot": 1}, "value": None}}

    proven = offline_proof("http://127.0.0.1:9999")
    fund_sol(proven, fresh_pubkey(), 7, rpc_call=transport)

    assert seen, "no request was made at all"
    assert {url for url, _method, _params in seen} == {"http://127.0.0.1:9999"}
    methods = [method for _url, method, _params in seen]
    assert "surfnet_setAccount" in methods


def test_the_set_account_params_are_the_measured_two_tuple() -> None:
    """Measured: ``[]`` → 'expected a tuple of size 2', 3 elements → 'fewer elements'."""
    sent: list[list[Any]] = []

    def transport(_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "surfnet_setAccount":
            sent.append(params)
        if method == "getBalance":
            return {"result": {"context": {"slot": 1}, "value": 42}}
        return {"result": {"context": {"slot": 1}, "value": None}}

    address = fresh_pubkey()
    fund_sol(offline_proof(), address, 42, rpc_call=transport)
    assert sent == [[address, {"lamports": 42}]]


def token_account_bytes(mint: str, owner: str, amount: int) -> str:
    """A base64 SPL token account — ``mint(32) | owner(32) | amount(u64 LE)``, padded.

    Built here rather than mocked away because the read-back decodes these exact bytes;
    a fake that returned a friendly dict would not exercise the decoder at all.
    """
    from base64 import b64encode

    from solders.pubkey import Pubkey

    raw = bytes(Pubkey.from_string(mint)) + bytes(Pubkey.from_string(owner))
    raw += amount.to_bytes(8, "little")
    return b64encode(raw.ljust(165, b"\0")).decode()


def test_the_token_params_carry_the_token_program_as_a_fourth_element() -> None:
    """Measured: 3 args minimum, and the 4th selects which ATA gets written."""
    sent: list[list[Any]] = []
    owner = fresh_pubkey()

    def transport(_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "surfnet_setTokenAccount":
            sent.append(params)
        if method == "getAccountInfo" and sent:
            return {
                "result": {
                    "context": {"slot": 1},
                    "value": {"data": [token_account_bytes(USDC, owner, 5), "base64"]},
                }
            }
        return {"result": {"context": {"slot": 1}, "value": None}}

    funded = fund_token(offline_proof(), owner, USDC, 5, rpc_call=transport)
    assert sent == [[owner, USDC, {"amount": 5}, funded.token_program]]
    assert len(sent[0]) == 4
    assert funded.created and funded.observed_amount == 5


def test_a_token_write_is_verified_by_bytes_not_by_the_response() -> None:
    """A token account that decodes to the wrong OWNER is refused, not reported as funded.

    ``getTokenAccountBalance`` cannot be the read-back — it needs the mint account, which
    this cheatcode does not create — so the check reads the account's own 72 leading
    bytes. Those carry the mint and the owner as well as the amount, which is why a node
    answering with some other token account's bytes is caught here rather than believed.
    """
    owner = fresh_pubkey()
    someone_else = fresh_pubkey()

    def wrong_account(_url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            return {
                "result": {
                    "context": {"slot": 1},
                    "value": {
                        "data": [token_account_bytes(USDC, someone_else, 5), "base64"]
                    },
                }
            }
        return {"result": {"context": {"slot": 1}, "value": None}}

    with pytest.raises(CheatcodeError) as excinfo:
        fund_token(offline_proof(), owner, USDC, 5, rpc_call=wrong_account)
    assert "did not take effect" in str(excinfo.value)


def test_a_write_that_did_not_take_effect_is_an_error_not_a_success() -> None:
    """The core anti-false-positive property: ``{"value": null}`` is not evidence.

    Surfpool answers every successful state write with a null value, and it silently
    ignores unknown update fields — so a typo and a real mutation look identical on the
    wire. The read-back is the only thing that separates them.
    """

    def lying_transport(_url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        if method == "getBalance":
            return {"result": {"context": {"slot": 1}, "value": 0}}
        return {"result": {"context": {"slot": 1}, "value": None}}

    with pytest.raises(CheatcodeError) as excinfo:
        fund_sol(offline_proof(), fresh_pubkey(), 1_000_000, rpc_call=lying_transport)
    assert "did not take effect" in str(excinfo.value)


def test_time_travel_demands_exactly_one_destination() -> None:
    """Measured: the server calls it 'expected map with a single key'."""
    proven = offline_proof()
    with pytest.raises(TimeTravelError) as none_given:
        time_travel(proven)
    with pytest.raises(TimeTravelError) as two_given:
        time_travel(proven, epoch=1, slot=2)
    assert "absoluteEpoch" in str(none_given.value)
    assert "MILLISECONDS" in str(two_given.value)


def test_a_backwards_jump_is_refused_before_it_is_sent() -> None:
    """The refusal is computed from the fork's own clock, not scraped from an error.

    Surfpool's explanation lives in ``error.data``, which the canonical transport drops
    on purpose — so this asserts that no ``surfnet_timeTravel`` request is issued at all
    for a destination the clock has already passed.
    """
    sent: list[str] = []

    def transport(_url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        sent.append(method)
        if method == "getEpochInfo":
            return {"result": {"epoch": 1018, "absoluteSlot": 439_900_000}}
        return {"result": {"context": {"slot": 1}, "value": None}}

    proven = offline_proof()
    with pytest.raises(TimeTravelError) as backwards:
        time_travel(proven, slot=439_000_000, rpc_call=transport)
    assert "forward-only" in str(backwards.value)
    assert "surfnet_timeTravel" not in sent


def test_unix_seconds_in_timestamp_ms_is_named_as_the_wrong_unit() -> None:
    """The measured trap, refused with the fix in the message rather than a -32603."""
    with pytest.raises(TimeTravelError) as excinfo:
        time_travel(offline_proof(), timestamp_ms=1_788_235_466)
    assert "unix SECONDS" in str(excinfo.value)
    assert "Multiply by 1000" in str(excinfo.value)


# --- the fork: assert the EFFECT ---------------------------------------------


@pytest.mark.fork
@needs_fork
def test_fund_sol_sets_an_absolute_balance_and_creates_a_missing_account(
    proof: SurfnetProof,
) -> None:
    """Read the balance back, twice, and prove the second call SETS rather than ADDS."""
    address = fresh_pubkey()

    first = fund_sol(proof, address, 5_000_000_000)
    assert first.observed_lamports == 5_000_000_000
    assert first.created, "a never-seen address should not have existed on the fork"

    second = fund_sol(proof, address, 1_000_000_000)
    assert second.observed_lamports == 1_000_000_000, "absolute, not additive"
    assert not second.created


@pytest.mark.fork
@needs_fork
def test_fund_sol_only_patches_lamports_and_leaves_the_rest_of_the_account(
    proof: SurfnetProof,
) -> None:
    """The measured surprise worth a test: the update struct is a PATCH, not a replace.

    Funding a token account's lamports must not wipe its 165 bytes or hand it back to
    the System Program — a rehearsal that funded a buyer and destroyed their ATA in the
    same call would be very hard to diagnose from a revert.
    """
    from gecko.rpc import default_rpc_call

    owner = fresh_pubkey()
    funded = fund_token(proof, owner, USDC, 250_000)
    fund_sol(proof, funded.token_account, 9_000_000)

    account = default_rpc_call(
        proof.rpc_url, "getAccountInfo", [funded.token_account, {"encoding": "base64"}]
    )["result"]["value"]
    assert account["lamports"] == 9_000_000
    assert account["owner"] == funded.token_program, "the patch reassigned the owner"
    assert account["space"] == 165, "the patch destroyed the token account's data"


@pytest.mark.fork
@needs_fork
def test_fund_token_creates_the_ata_when_it_does_not_exist(
    proof: SurfnetProof,
) -> None:
    """THE question this goal had to answer, asserted against a validator.

    The owner is a keypair generated microseconds ago, so its ATA cannot exist on
    mainnet or on the fork. If ``surfnet_setTokenAccount`` needed the ATA to be there
    first, this is where that shows up — the balance read-back inside ``fund_token``
    would raise before the assertions below are reached.
    """
    from gecko.rpc import default_rpc_call

    owner = fresh_pubkey()
    price = 120_000  # exactly one product's price, which is the rehearsal's whole need

    funded = fund_token(proof, owner, USDC, price)

    assert funded.created, "the ATA existed already — this test proved nothing"
    assert funded.observed_amount == price
    account = default_rpc_call(
        proof.rpc_url,
        "getAccountInfo",
        [funded.token_account, {"encoding": "jsonParsed"}],
    )["result"]["value"]
    info = account["data"]["parsed"]["info"]
    assert info["owner"] == owner
    assert info["mint"] == USDC
    assert info["state"] == "initialized", "created but not initialized is unusable"
    assert account["lamports"] == 2_039_280, "the created ATA must be rent-exempt"


@pytest.mark.fork
@needs_fork
def test_fund_token_is_absolute_so_a_buyer_can_hold_exactly_the_price(
    proof: SurfnetProof,
) -> None:
    owner = fresh_pubkey()
    fund_token(proof, owner, USDC, 4_200_000)
    exact = fund_token(proof, owner, USDC, 120_000)
    assert exact.observed_amount == 120_000, "absolute, not additive"


@pytest.mark.fork
@needs_fork
def test_fund_token_writes_the_token_2022_ata_when_that_program_is_named(
    proof: SurfnetProof,
) -> None:
    """The 4th parameter moves the ADDRESS, so it is checked by address, not by echo."""
    from gecko.rpc import default_rpc_call
    from gecko.store_accounts import derive_ata

    owner = fresh_pubkey()
    mint = fresh_pubkey()

    funded = fund_token(proof, owner, mint, 77, token_program=TOKEN_2022)

    assert funded.token_account == derive_ata(owner, mint, token_program=TOKEN_2022)
    legacy = default_rpc_call(
        proof.rpc_url,
        "getAccountInfo",
        [derive_ata(owner, mint), {"encoding": "base64"}],
    )["result"]["value"]
    assert legacy is None, "the legacy ATA must be untouched when 2022 was named"


@pytest.mark.fork
@needs_fork
def test_fund_token_does_not_create_the_mint_behind_the_balance(
    proof: SurfnetProof,
) -> None:
    """The honest limit, pinned: the balance can be backed by no mint at all.

    A fork state that is arithmetically impossible will still simulate as if it were
    real up to the point an instruction reads the mint. Anyone reading this test knows
    not to conclude 'the token exists' from 'the buyer has a balance'.
    """
    from gecko.rpc import default_rpc_call

    owner = fresh_pubkey()
    mint = fresh_pubkey()

    funded = fund_token(proof, owner, mint, 123)

    assert funded.observed_amount == 123
    mint_account = default_rpc_call(
        proof.rpc_url, "getAccountInfo", [mint, {"encoding": "base64"}]
    )["result"]["value"]
    assert mint_account is None, (
        "surfpool started creating mints — update the docstring"
    )


@pytest.mark.fork
@needs_fork
def test_reset_account_falls_back_to_the_forked_chain_rather_than_emptying(
    proof: SurfnetProof,
) -> None:
    """The name overpromises, so the test states what actually happens.

    A fresh address has nothing on mainnet, so after the reset it is ABSENT — not zero,
    and not the 5 SOL that was written locally. The distinction is the whole point:
    reset is 'undo my cheatcodes', not 'empty this account'.
    """
    address = fresh_pubkey()
    fund_sol(proof, address, 5_000_000_000)

    outcome = reset_account(proof, address)

    assert outcome.lamports_before == 5_000_000_000
    assert outcome.lamports_after is None, (
        "the override survived the reset, or reset zeroed instead of falling back"
    )


@pytest.mark.fork
@needs_fork
def test_time_travel_moves_the_clock_forward_and_refuses_to_go_back(
    proof: SurfnetProof,
) -> None:
    """Assert the landing slot, then assert the backwards refusal from that same clock."""
    from gecko.rpc import default_rpc_call

    before = default_rpc_call(proof.rpc_url, "getEpochInfo", [])["result"]
    target = int(before["absoluteSlot"]) + 500_000

    jumped = time_travel(proof, slot=target)

    assert jumped.absolute_slot >= target
    now = default_rpc_call(proof.rpc_url, "getEpochInfo", [])["result"]
    assert int(now["absoluteSlot"]) >= target, "the clock did not actually move"

    with pytest.raises(TimeTravelError) as excinfo:
        time_travel(proof, slot=int(before["absoluteSlot"]) - 1_000_000)
    assert "forward-only" in str(excinfo.value)


@pytest.mark.fork
@needs_fork
def test_time_travel_by_epoch_and_the_millisecond_timestamp_trap(
    proof: SurfnetProof,
) -> None:
    """``absoluteTimestamp`` is MILLISECONDS, and the fork's clock is its own.

    Jumping two epochs first puts the fork's internal clock ~2 days ahead of wallclock;
    the ms value for "now" is then genuinely in its past, so the SERVER refuses it — the
    -32603 whose reason lives in the dropped ``error.data``. That is the leg that has to
    run against a validator. (The wrong-UNIT case is caught locally and tested offline;
    it never reaches the wire.)
    """
    import time as wallclock

    from gecko.rpc import default_rpc_call

    before = default_rpc_call(proof.rpc_url, "getEpochInfo", [])["result"]
    landed = time_travel(proof, epoch=int(before["epoch"]) + 2)
    assert landed.epoch == int(before["epoch"]) + 2

    now_ms = int(wallclock.time() * 1000)
    with pytest.raises(TimeTravelError) as past_exc:
        time_travel(proof, timestamp_ms=now_ms)
    assert "-32603" in str(past_exc.value)

    # +1 day in MS from the fork's own (already advanced) position, expressed as a slot
    # count so the assertion does not depend on which clock is further ahead.
    ahead = time_travel(proof, slot=landed.absolute_slot + 216_000)
    assert ahead.absolute_slot - landed.absolute_slot >= 216_000


@pytest.mark.fork
@needs_fork
def test_time_travel_does_not_age_a_blockhash(proof: SurfnetProof) -> None:
    """The finding that changes how the rehearsal must prove expiry.

    Jumping whole epochs moves the reported SLOT by millions and ``getBlockHeight`` by a
    handful — block height tracks the fork's own produced blocks. So the expiry deadline
    (``lastValidBlockHeight`` measured against block height) cannot be reached by time
    travel, and any test that tries to expire a blockhash this way would pass for the
    wrong reason.
    """
    from gecko.landing import latest_blockhash
    from gecko.rpc import default_rpc_call

    slot_before = int(
        default_rpc_call(proof.rpc_url, "getEpochInfo", [])["result"]["absoluteSlot"]
    )
    _hash_before, last_valid_before = latest_blockhash(proof.rpc_url)

    jumped = time_travel(proof, epoch=jump_target(proof))

    _hash_after, last_valid_after = latest_blockhash(proof.rpc_url)
    slots_travelled = jumped.absolute_slot - slot_before
    assert slots_travelled > 1_000_000, "the jump was too small to prove anything"
    assert last_valid_after - last_valid_before < 1_000, (
        f"the slot clock moved {slots_travelled} but the blockhash deadline moved "
        f"{last_valid_after - last_valid_before} — if that ever tracks the slot jump, "
        "the module docstring's claim that time travel cannot age a blockhash is wrong "
        "and the rehearsal's expiry reasoning has to change with it"
    )


def jump_target(proof: SurfnetProof) -> int:
    """Five epochs past wherever this fork currently is."""
    from gecko.rpc import default_rpc_call

    epoch = default_rpc_call(proof.rpc_url, "getEpochInfo", [])["result"]["epoch"]
    return int(epoch) + 5
