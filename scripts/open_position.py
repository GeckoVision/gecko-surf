#!/usr/bin/env python3
"""Open an Orca Whirlpool liquidity position on mainnet and fund it, signed by the wallet.

Two transactions, in order, both self-paid by the wallet (the position's rent is about
0.006 SOL, refunded on close, and a fee relay capped at 0.001 SOL per transaction cannot
pay it):

1. ``open_position``: a fresh position-mint key signs its own slot once and is dropped;
   the wallet signs as funder and owner.
2. ``increase_liquidity_by_token_amounts_v2``: the wallet deposits up to the two maxima
   you name; the program sizes the liquidity and reverts outside the sqrt-price bounds.

Each transaction passes the same path every Gecko spend passes: simulate the exact bytes,
verify the binding, the spend gate with its own policy (one instruction allowlisted,
the rent bound on the first, the per-mint caps and the operator's USDG acceptance on the
second), then the wallet signs. The dry run plans both and simulates the first; the
second can only simulate once the position exists.

    uv run python scripts/open_position.py --network mainnet --rpc-url "$RPC_URL" \\
        --signer paybox --token-max-a 5000 --token-max-b 5000            # dry run
    # add --broadcast to open and fund it, founder-authorized
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from solders.keypair import Keypair  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from gecko.autonomous_purchase import PurchaseSettled, settle_cosigned  # noqa: E402
from gecko.cosign import normalize_signature_slots  # noqa: E402
from gecko.decision_log import append_decision, decision_row  # noqa: E402
from gecko.networks import coerce_network  # noqa: E402
from gecko.orquestra_build import orquestra_seams  # noqa: E402
from gecko.prepare_instruction import prepare_instruction_result  # noqa: E402
from gecko.providers.whirlpool import WHIRLPOOL_PROGRAM  # noqa: E402
from gecko.providers.whirlpool_position import (  # noqa: E402
    complete_increase_data,
    INCREASE_BY_AMOUNTS,
    OPEN_POSITION,
    plan_position,
    position_spend_policies,
)
from gecko.rpc import default_rpc_call, validate_rpc_url  # noqa: E402
from gecko.signer import (  # noqa: E402
    FEE_PAYER_ROLE,
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    TransactionSigner,
)
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.store_accounts import derive_ata  # noqa: E402
from gecko.spend_policy import InMemorySpendLedger, SpendPolicyGate  # noqa: E402
from gecko.token_program import read_mint_extensions  # noqa: E402
from gecko.trace import Trace  # noqa: E402
from operator_policy import USDG_ACCEPTED  # noqa: E402
from scripts.paybox_backend import PayboxAuthorityBackend, PayboxBackendError  # noqa: E402

#: The USDG/USDC Whirlpool the route swaps through (tick spacing 1, 0.01% fee).
DEFAULT_POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
#: The wallet keeps this much SOL above the rent bound, for the two fees.
MIN_SOL_LAMPORTS = 8_000_000


def _rpc(url: str, method: str, params: list[Any]) -> Any:
    return default_rpc_call(url, method, params).get("result")


def _sol(url: str, owner: str) -> int:
    return int(_rpc(url, "getBalance", [owner, {"commitment": "confirmed"}])["value"])


def _held(url: str, owner: str, mint: str) -> tuple[int, int]:
    """(raw held, decimals) of one mint, summed over the owner's accounts."""
    rows = _rpc(
        url,
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )["value"]
    raw, decimals = 0, None
    for row in rows:
        amount = row["account"]["data"]["parsed"]["info"]["tokenAmount"]
        raw += int(amount["amount"])
        decimals = int(amount["decimals"])
    if decimals is None:
        info = _rpc(url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
        decimals = int(info["data"]["parsed"]["info"]["decimals"])
    return raw, decimals


def _signer(
    buyer: Any, network: Any, gate: SpendPolicyGate, paybox: bool
) -> TransactionSigner:
    return TransactionSigner(
        backend=buyer,
        profile=SignerProfile(
            name=EXTERNAL_SIGNER_PROFILE_NAME
            if paybox
            else DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
            network=network,
            authorized=True,
            signing_as=FEE_PAYER_ROLE,  # self-paid: the wallet pays the fee and signs as owner
        ),
        spend_gate=gate,
    )


def _record(
    network: Any,
    trace: Trace,
    *,
    terminal: str,
    code: str | None,
    signatures: tuple[str, ...],
    at_stake: tuple[tuple[str, int, int], ...],
) -> None:
    row = decision_row(
        trace=trace,
        lane="position",
        network=str(network),
        programs=("whirlpool",),
        terminal=terminal,  # type: ignore[arg-type]
        code=code,
        signatures=signatures,
        at_stake=at_stake,
    )
    print(
        f"  decision   {terminal}{f' [{code}]' if code else ''}  -> {append_decision(row)}"
    )


def _emit(trace: Trace, path: Path | None) -> None:
    if path is not None:
        trace.write(path)
        print(f"  trace      {path}  ({len(trace.rows)} steps)")


def _gate(policy: Any) -> SpendPolicyGate:
    return SpendPolicyGate(policy=policy, ledger=InMemorySpendLedger())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--network", choices=["mainnet"], required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--signer", choices=["paybox", "keypair"], default="paybox")
    parser.add_argument("--buyer-keypair", type=Path, default=None)
    parser.add_argument("--paybox-credential", default=None)
    parser.add_argument("--pool", default=DEFAULT_POOL)
    parser.add_argument(
        "--token-max-a", type=int, required=True, help="raw units of mint A"
    )
    parser.add_argument(
        "--token-max-b", type=int, required=True, help="raw units of mint B"
    )
    parser.add_argument("--slippage-bps", type=int, default=100)
    parser.add_argument(
        "--position-mint",
        default=None,
        help="fund a position this wallet already opened (printed by a run that landed "
        "the open) instead of opening a second one",
    )
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--trace", type=Path, default=None)
    args = parser.parse_args(argv)

    validate_rpc_url(args.rpc_url)
    network = coerce_network(args.network)
    if args.signer == "paybox":
        try:
            buyer: Any = PayboxAuthorityBackend.open(
                credential=args.paybox_credential, self_paid=True
            )
        except PayboxBackendError as exc:
            print(f"STOP: PayBox signer not usable: {exc}")
            return 2
        print(
            f"  paybox wallet      {buyer.wallet.name} ({buyer.wallet.approval_mode})"
        )
    else:
        from scripts.gasless_purchase import AuthorityKeypairBackend  # type: ignore[attr-defined]

        if args.buyer_keypair is None:
            print("STOP: --signer keypair needs --buyer-keypair")
            return 2
        buyer = AuthorityKeypairBackend(args.buyer_keypair)
    owner = buyer.pubkey
    print(f"  owner (signer)     {owner}")

    fresh = args.position_mint is None
    position_mint = Keypair() if fresh else None
    mint_address = (
        str(position_mint.pubkey()) if position_mint else str(args.position_mint)
    )
    position, bump = Pubkey.find_program_address(
        [b"position", bytes(Pubkey.from_string(mint_address))],
        Pubkey.from_string(WHIRLPOOL_PROGRAM),
    )
    position_token_account = derive_ata(
        owner, mint_address, token_program=TOKEN_PROGRAM
    )
    idl_fetch, build_call = orquestra_seams()
    plan = plan_position(
        {
            "pool": args.pool,
            "owner": owner,
            "position_mint": mint_address,
            "position_bump": bump,
            "token_max_a": args.token_max_a,
            "token_max_b": args.token_max_b,
            "slippage_bps": args.slippage_bps,
        },
        rpc_url=args.rpc_url,
        idl_fetch=idl_fetch,
    )
    mint_a, mint_b = plan["mints"]["a"], plan["mints"]["b"]
    held_a, dec_a = _held(args.rpc_url, owner, mint_a)
    held_b, dec_b = _held(args.rpc_url, owner, mint_b)
    sol = _sol(args.rpc_url, owner)
    print(
        f"  pool       {args.pool[:8]}…  tick {plan['tick_current']} spacing "
        f"{plan['tick_spacing']}  range [{plan['tick_lower']}, {plan['tick_upper']})"
    )
    print(
        f"  wallet     SOL {sol}  A {held_a} (max {args.token_max_a})  B {held_b} (max {args.token_max_b})"
    )
    stake = ((mint_a, args.token_max_a, dec_a), (mint_b, args.token_max_b, dec_b))
    needs_sol = MIN_SOL_LAMPORTS if fresh else 100_000
    if held_a < args.token_max_a or held_b < args.token_max_b or sol < needs_sol:
        print(
            "STOP: the wallet holds less than the deposit maxima or the SOL for rent; nothing guesses smaller amounts"
        )
        _record(
            network,
            Trace(lane="position", network=str(network)),
            terminal="abandoned",
            code="position-unfunded",
            signatures=(),
            at_stake=stake,
        )
        return 2

    evidence = read_mint_extensions([mint_a, mint_b], rpc_url=args.rpc_url)
    accepted = (
        {USDG_ACCEPTED.mint: USDG_ACCEPTED}
        if USDG_ACCEPTED.mint in (mint_a, mint_b)
        else {}
    )
    for mint, acceptance in accepted.items():
        read = evidence.get(mint)
        if read is None or not acceptance.matches(read):
            print(
                f"STOP: {mint[:8]}… no longer reads as the accepted state; a human looks first"
            )
            _record(
                network,
                Trace(lane="position", network=str(network)),
                terminal="abandoned",
                code="mint-extensions-changed",
                signatures=(),
                at_stake=stake,
            )
            return 2

    increase_values = dict(plan["increase_values"])
    increase_values["position"] = str(position)
    increase_values["position_token_account"] = position_token_account
    increase_destinations = frozenset(
        increase_values[k]
        for k in (
            "whirlpool",
            "position",
            "token_owner_account_a",
            "token_owner_account_b",
            "token_vault_a",
            "token_vault_b",
            "tick_array_lower",
            "tick_array_upper",
        )
    )
    # The open leg's allowlist is OUR derivation, never the builder's echo of its own work.
    open_policy, increase_policy = position_spend_policies(
        plan,
        open_destinations=frozenset(
            {str(position), mint_address, position_token_account}
        ),
        increase_destinations=increase_destinations,
        decimals={mint_a: dec_a, mint_b: dec_b},
        accepted_mints=tuple(accepted.values()),
    )

    opened: dict[str, Any] | None = None
    if fresh:
        opened = prepare_instruction_result(
            {
                "program_id": WHIRLPOOL_PROGRAM,
                "instruction": OPEN_POSITION,
                "payer": owner,
                "values": plan["open_values"],
            },
            idl_fetch=idl_fetch,
            build_call=build_call,
            rpc_url=args.rpc_url,
        )
        if opened.get("refused"):
            print(
                f"REFUSED at prepare (open) [{opened.get('code')}]: {opened.get('reason')}"
            )
            return 1
        accounts = dict(opened["accounts"])
        for name, ours in (
            ("position", str(position)),
            ("position_mint", mint_address),
            ("position_token_account", position_token_account),
        ):
            if accounts.get(name) != ours:
                print(
                    f"STOP: the builder's {name} is not the one derived here; nothing is signed"
                )
                return 1
        open_subject = base64.b64encode(
            normalize_signature_slots(
                base64.b64decode(str(opened["transaction_base64"]))
            )
        ).decode()
        receipt = simulate(
            {},
            rpc_url=args.rpc_url,
            build_call=lambda _plan: BuiltTx(tx=open_subject, encoding="base64"),
            replace_blockhash=False,
            network_label=f"simulated against {network} (open, unsigned)",
            network=network,
            track=[owner],
        )
        print(
            f"\n  OPEN     {receipt.status.upper()}  {receipt.units_consumed or 0:,} CU  "
            f"SOL {receipt.sol_delta} (rent + fee)  position {str(position)[:8]}…"
        )
        if receipt.status != "pass":
            print(
                f"REFUSED: the open transaction does not simulate ({receipt.revert_class})"
            )
            return 1
        verdict = _gate(open_policy).authorize(open_subject, receipt)
        print(
            f"  gate     open {'AUTHORIZED' if verdict.authorized else 'REFUSED'}"
            f"{'' if verdict.authorized else f' [{verdict.code}] {verdict.reason}'}"
        )
        if not verdict.authorized:
            return 1
    else:
        exists = _rpc(
            args.rpc_url, "getAccountInfo", [str(position), {"encoding": "base64"}]
        )
        if not (exists or {}).get("value"):
            print(f"STOP: no position exists at {position} for mint {mint_address}")
            return 2
        print(f"\n  OPEN     skipped: position {str(position)[:8]}… already exists")

    increase = prepare_instruction_result(
        {
            "program_id": WHIRLPOOL_PROGRAM,
            "instruction": INCREASE_BY_AMOUNTS,
            "payer": owner,
            "values": increase_values,
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
        rpc_url=args.rpc_url,
    )
    if increase.get("refused"):
        print(
            f"REFUSED at prepare (deposit) [{increase.get('code')}]: {increase.get('reason')}"
        )
        return 1
    increase_tx = complete_increase_data(str(increase["transaction_base64"]), plan)
    print(
        "  DEPOSIT  prepared"
        + ("; it simulates once the position exists" if fresh else "")
    )

    if not fresh:
        deposit_subject = base64.b64encode(
            normalize_signature_slots(base64.b64decode(increase_tx))
        ).decode()
        deposit_receipt = simulate(
            {},
            rpc_url=args.rpc_url,
            build_call=lambda _plan: BuiltTx(tx=deposit_subject, encoding="base64"),
            replace_blockhash=False,
            network_label=f"simulated against {network} (deposit, unsigned)",
            network=network,
            track=[owner],
            mint_extensions=evidence,
            accepted_mints=accepted,
        )
        print(
            f"  DEPOSIT  {deposit_receipt.status.upper()}  "
            f"{deposit_receipt.units_consumed or 0:,} CU"
        )
        if deposit_receipt.status != "pass":
            print(
                f"REFUSED: the deposit does not simulate ({deposit_receipt.revert_class}): "
                f"{' | '.join(deposit_receipt.logs_tail[-2:])}"
            )
            return 1
        verdict = _gate(increase_policy).authorize(deposit_subject, deposit_receipt)
        print(
            f"  gate     deposit {'AUTHORIZED' if verdict.authorized else 'REFUSED'}"
            f"{'' if verdict.authorized else f' [{verdict.code}] {verdict.reason}'}"
        )
        if not verdict.authorized:
            return 1

    if not args.broadcast:
        print(
            "\nDRY RUN: the position is planned"
            + (", the open simulated and gated" if fresh else "")
            + "; nothing was signed."
        )
        print(
            "Re-run with --broadcast to "
            + ("open and fund it." if fresh else "fund it.")
        )
        return 0

    paybox = args.signer == "paybox"
    trace = Trace(lane="position", network=str(network))
    signatures: list[str] = []
    if fresh:
        assert opened is not None and position_mint is not None
        first = settle_cosigned(
            str(opened["transaction_base64"]),
            network=network,
            rpc_url=args.rpc_url,
            signer=_signer(buyer, network, _gate(open_policy), paybox),
            authority=owner,
            cosigners=[position_mint],
            trace=trace,
        )
        if not isinstance(first, PurchaseSettled):
            print(f"  open       REFUSED [{first.code}]: {first.reason}")
            _emit(trace, args.trace)
            _record(
                network,
                trace,
                terminal="refused",
                code=first.code,
                signatures=(),
                at_stake=stake,
            )
            return 1
        signatures.append(first.signature)
        print(
            f"  open       LANDED {first.signature}  CU predicted {first.predicted_units} charged {first.consumed_units}"
        )
        print(
            f"  position   {position}  (mint {mint_address}; re-run with --position-mint to fund it)"
        )
        increase = prepare_instruction_result(
            {
                "program_id": WHIRLPOOL_PROGRAM,
                "instruction": INCREASE_BY_AMOUNTS,
                "payer": owner,
                "values": increase_values,
            },
            idl_fetch=idl_fetch,
            build_call=build_call,
            rpc_url=args.rpc_url,
        )
        if increase.get("refused"):
            print(
                f"  deposit    REFUSED at prepare [{increase.get('code')}]: {increase.get('reason')}"
            )
            _emit(trace, args.trace)
            _record(
                network,
                trace,
                terminal="refused",
                code="prepare-refused",
                signatures=(),
                at_stake=stake,
            )
            return 1
        increase_tx = complete_increase_data(str(increase["transaction_base64"]), plan)
    second = settle_cosigned(
        increase_tx,
        network=network,
        rpc_url=args.rpc_url,
        signer=_signer(buyer, network, _gate(increase_policy), paybox),
        authority=owner,
        mint_extensions=evidence,
        accepted_mints=accepted,
        trace=trace,
    )
    _emit(trace, args.trace)
    if not isinstance(second, PurchaseSettled):
        print(f"  deposit    REFUSED [{second.code}]: {second.reason}")
        _record(
            network,
            trace,
            terminal="refused",
            code=second.code,
            signatures=tuple(signatures),
            at_stake=stake,
        )
        print(
            f"  the position is open and empty; fund it with --position-mint {mint_address}"
        )
        return 1
    signatures.append(second.signature)
    print(
        f"  deposit    LANDED {second.signature}  CU predicted {second.predicted_units} charged {second.consumed_units}"
    )
    moved = [
        (o.mint[:8], o.raw)
        for o in (
            second.receipt.token_delta.outflows() if second.receipt.token_delta else ()
        )
        if o.owner == owner
    ]
    print(f"  deposited  {moved}  into position {position}")
    _record(
        network,
        trace,
        terminal="landed",
        code=None,
        signatures=tuple(signatures),
        at_stake=stake,
    )
    print(
        json.dumps(
            {
                "position": str(position),
                "position_mint": mint_address,
                "signatures": signatures,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
