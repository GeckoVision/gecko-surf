"""Prepare an Orca Whirlpool swap for signing — derive, simulate, bind, and STOP.

The swap twin of ``scripts/prepare_purchase.py``: it plans the call, reads every live fact
it cannot derive, simulates against the network you name, and prints the bytes plus the
BINDING. It never signs and never broadcasts. ``scripts/sign_and_send.py`` is the only
file in this repository that can sign a mainnet transaction.

    # prepare only — derives, simulates, prints the bytes and the binding. Signs nothing.
    uv run python scripts/prepare_whirlpool_swap.py --signer <YOUR_PUBKEY>

    # prepare AND settle, in one call — no window for the blockhash to expire in
    uv run python scripts/prepare_whirlpool_swap.py --signer <YOUR_PUBKEY> \
        --keypair ~/.config/solana/id.json --send

TWO MODES, AND WHEN EACH IS RIGHT. Without ``--send`` this is a twin of
``prepare_purchase.py``: it hands you bytes plus a binding to carry to
``sign_and_send.py`` in another terminal. That flow has the stronger substitution check —
see below — and is right whenever the bytes have a journey to make.

With ``--send`` it is a twin of ``autonomous_purchase.py``: prepare, verify, sign, settle,
in one process. Use it because a blockhash lives about a minute and a human copying two
long base58 strings between terminals outlives it — measured, on the first real run: the
transaction was correct and the pre-flight refused it anyway because it had gone stale.
``_settle`` states exactly what that trade does and does not give up.

WHY THE BINDING TRAVELS WITH THE BYTES. ``sign_and_send.py`` runs four checks and three of
them are self-referential — the fresh receipt is computed FROM the bytes in ``--tx``, so
"does this receipt attest these bytes" asks whether a value equals itself, and a
transaction with the recipient rewritten simulates perfectly and passes. Only
``--expect-binding``, recorded HERE before the bytes travel, has two sides with different
origins. Carry it or the handoff proves nothing.

WHAT IS READ RATHER THAN ASSUMED, because none of it is in the IDL:

* Each mint's TOKEN PROGRAM, from the mint account's own ``owner``. USDG is Token-2022 and
  USDC is classic SPL, so this swap runs two different token programs at once and the two
  ATAs derive under different second seeds. Defaulting here is the single most common way
  to produce a valid, empty, never-initialised account.
* ``token_vault_a`` / ``token_vault_b``, which live INSIDE the whirlpool account.
* The three TICK ARRAYS, which depend on ``tick_current_index`` AND on the DIRECTION of
  travel: b->a walks ticks up, a->b walks them down, so the two directions need different
  arrays. Only the array containing the current tick is shared.
* ``sqrt_price_limit``, derived from the pool's live ``sqrt_price``. Orca's documented
  MAX_SQRT_PRICE is rejected by the program (measured: AnchorError SqrtPriceOutOfBounds),
  and a limit at the extreme would mean NO slippage protection anyway — which is never
  what you want with real money.

WHAT A SMALL SWAP HERE DOES NOT PROVE. This pool holds ~$25M at tick_spacing=1, so a test
-sized swap never leaves the tick array it starts in and therefore does not exercise
multi-array TRAVERSAL. Traversal is a construction property and is proved on a fork, at
whatever size crosses arrays, for free. Do not read a green mainnet swap as covering it.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.handoff import verify_handoff  # noqa: E402
from gecko.idl_layout import field_offset  # noqa: E402
from gecko.networks import NETWORKS, coerce_network  # noqa: E402
from gecko.pda import b58_encode, derive_pda  # noqa: E402
from gecko.prepare_instruction import prepare_instruction_result  # noqa: E402
from gecko.provider_config import load_packaged_provider  # noqa: E402
from gecko.providers.catalog_surface import orquestra_seams  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.store_accounts import derive_ata  # noqa: E402

DEFAULT_KEYPAIR = Path.home() / ".config" / "solana" / "id.json"

WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
#: The USDG/USDC pool: tick_spacing 1, fee_rate 100 (0.01%), the only USDG/USDC venue with
#: both real depth (~$25.68M) and an ingestible on-chain IDL.
DEFAULT_POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"
#: Whirlpool packs 88 ticks per array, whatever the spacing.
TICKS_PER_ARRAY = 88
_SIZES = {"pubkey": 32, "u128": 16, "u64": 8, "i32": 4, "u32": 4, "u16": 2, "u8": 1}


def _rpc(url: str, method: str, params: list, *, tries: int = 4) -> dict:
    """Retry the reads. The public mainnet endpoint answers 429 and bare timeouts under
    any real use, and a prepare that dies on the third of seven reads wastes the minute
    the blockhash is good for. Failing loudly after several honest attempts is different
    from failing on the first."""
    for attempt in range(tries):
        try:
            return default_rpc_call(url, method, params)
        except Exception as exc:  # noqa: BLE001 - transport flakiness, not an answer
            if attempt == tries - 1:
                raise SystemExit(
                    f"\nSTOP: {method} failed after {tries} attempts against {url}"
                    f"\n  {type(exc).__name__}: {exc}"
                    "\n  A public endpoint rate-limits under any real use. Pass a dedicated"
                    "\n  --rpc-url (Helius/Alchemy/QuickNode) and re-run; re-running is free."
                ) from exc
            time.sleep(1.5 * (attempt + 1))
    return {}


def _pool_fields(idl: dict, raw: bytes, names: list[str]) -> dict:
    """Decode named Whirlpool fields at offsets the IDL computes — never hand-counted."""
    out: dict[str, object] = {}
    for name in names:
        loc = field_offset(idl, "Whirlpool", name)
        kind = loc["type"]
        chunk = raw[loc["offset"] : loc["offset"] + _SIZES[kind]]
        out[name] = (
            b58_encode(chunk)
            if kind == "pubkey"
            else int.from_bytes(chunk, "little", signed=kind.startswith("i"))
        )
    return out


def _token_program(url: str, mint: str) -> str:
    """The token program is the mint account's OWNER. Read, never inferred from a label."""
    value = (_rpc(url, "getAccountInfo", [mint, {"encoding": "base64"}]).get("result") or {}).get("value")
    if not value:
        raise SystemExit(f"STOP: mint {mint} does not exist on this node.")
    return value["owner"]


def _tick_arrays(pool: str, tick_current: int, tick_spacing: int, *, upward: bool) -> list[str]:
    """The three arrays in the DIRECTION OF TRAVEL, from our own corrected recipe.

    The seed is the ASCII DECIMAL STRING of the start index, not its little-endian bytes —
    the IDL declares the arg as i32 and an arg's type does not determine its seed encoding.
    """
    _, apis = load_packaged_provider("orquestra")
    recipe = dict(apis["whirlpool"].program.pdas)["tick_array"]
    span = TICKS_PER_ARRAY * tick_spacing
    start = (tick_current // span) * span
    steps = [start + span * i for i in range(3)] if upward else [start - span * i for i in range(3)]
    return [
        derive_pda(recipe, {"whirlpool": pool, "start_tick_index": str(s)}).address
        for s in steps
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepare_whirlpool_swap",
        description="Derive, simulate and BIND an Orca Whirlpool swap. Never signs.",
    )
    parser.add_argument("--signer", required=True, help="your base58 pubkey; pays fees AND is token_authority")
    parser.add_argument("--rpc-url", default="https://api.mainnet-beta.solana.com")
    parser.add_argument(
        "--network",
        default="mainnet",
        choices=sorted(NETWORKS),
        help=(
            "what --rpc-url points at. YOU say; nothing guesses — a fork proxy answers at "
            "any hostname, so the URL is evidence of nothing. Constrained here because "
            "`coerce_network` maps an unrecognised string to 'unknown' rather than raising, "
            "so a typo would otherwise travel silently to the gate."
        ),
    )
    parser.add_argument("--pool", default=DEFAULT_POOL)
    parser.add_argument(
        "--direction",
        choices=("b-to-a", "a-to-b"),
        default="b-to-a",
        help="b-to-a spends token B (USDC) for A (USDG); a-to-b is the reverse. Default b-to-a: it needs only USDC.",
    )
    parser.add_argument("--amount", type=int, default=100_000, help="input amount in the INPUT mint's base units (default 0.1 of a 6-decimal stable)")
    parser.add_argument("--slippage-bps", type=int, default=100, help="how far the sqrt price may move before the program refuses (default 100)")
    parser.add_argument(
        "--send",
        action="store_true",
        help="sign with --keypair and BROADCAST, in this one call. Without it nothing is signed.",
    )
    parser.add_argument(
        "--keypair",
        type=Path,
        default=DEFAULT_KEYPAIR,
        help="key to sign with when --send is passed. Never printed, never leaves this process.",
    )
    args = parser.parse_args(argv)

    url = args.rpc_url
    idl_fetch, build_call = orquestra_seams()
    idl = idl_fetch(WHIRLPOOL_PROGRAM)

    value = (_rpc(url, "getAccountInfo", [args.pool, {"encoding": "base64"}]).get("result") or {}).get("value")
    if not value:
        return _stop(f"pool {args.pool} does not exist on {url}")
    if value["owner"] != WHIRLPOOL_PROGRAM:
        return _stop(f"{args.pool} is owned by {value['owner']}, not the Whirlpool program")

    pool = _pool_fields(
        idl,
        base64.b64decode(value["data"][0]),
        ["tick_spacing", "fee_rate", "sqrt_price", "tick_current_index",
         "token_mint_a", "token_vault_a", "token_mint_b", "token_vault_b"],
    )
    mint_a, mint_b = str(pool["token_mint_a"]), str(pool["token_mint_b"])
    program_a, program_b = _token_program(url, mint_a), _token_program(url, mint_b)
    upward = args.direction == "b-to-a"
    live = int(pool["sqrt_price"])
    # Bound the sqrt price in the direction of travel. MAX/MIN would mean no protection.
    limit = (
        live * (10_000 + args.slippage_bps) // 10_000
        if upward
        else live * (10_000 - args.slippage_bps) // 10_000
    )
    ticks = _tick_arrays(args.pool, int(pool["tick_current_index"]), int(pool["tick_spacing"]), upward=upward)

    print(f"pool           {args.pool}")
    print(f"  tick_spacing {pool['tick_spacing']}   fee_rate {pool['fee_rate']}   tick_current {pool['tick_current_index']}")
    print(f"  mint A       {mint_a}  under {program_a}")
    print(f"  mint B       {mint_b}  under {program_b}")
    print(f"  vaults       {pool['token_vault_a']} / {pool['token_vault_b']}")
    print(f"\ndirection      {args.direction}  ({'B->A, price rises' if upward else 'A->B, price falls'})")
    print(f"  amount in    {args.amount} base units of {'B' if upward else 'A'}")
    print(f"  sqrt_price   {live}  ->  limit {limit}  ({args.slippage_bps} bps)")
    print(f"  tick arrays  {ticks}")

    ata_a = derive_ata(args.signer, mint_a, token_program=program_a)
    ata_b = derive_ata(args.signer, mint_b, token_program=program_b)

    # swap_v2 does NOT create token accounts: BOTH sides must already exist, including the
    # one you are only receiving into. Anchor reports the miss as 3012 AccountNotInitialized
    # against a slot name, which is accurate and tells you nothing about how to fix it — so
    # check here, where the answer is one command, rather than spending a simulation on it.
    for label, ata, mint, program in (
        ("A", ata_a, mint_a, program_a),
        ("B", ata_b, mint_b, program_b),
    ):
        exists = (
            _rpc(url, "getAccountInfo", [ata, {"encoding": "base64"}]).get("result") or {}
        ).get("value") is not None
        state = "exists" if exists else "MISSING"
        print(f"  your ATA {label}   {ata}  ({state})")
        if not exists:
            print(
                f"      create it first — it is rent, about 0.002 SOL, paid once:\n"
                f"      spl-token create-account {mint} --program-id {program} --owner {args.signer}"
            )

    result = prepare_instruction_result(
        {
            "program_id": WHIRLPOOL_PROGRAM,
            "instruction": "swap_v2",
            "payer": args.signer,
            "values": {
                "whirlpool": args.pool,
                "token_mint_a": mint_a,
                "token_mint_b": mint_b,
                "token_program_a": program_a,
                "token_program_b": program_b,
                "token_owner_account_a": ata_a,
                "token_owner_account_b": ata_b,
                "token_vault_a": pool["token_vault_a"],
                "token_vault_b": pool["token_vault_b"],
                "tick_array_0": ticks[0],
                "tick_array_1": ticks[1],
                "tick_array_2": ticks[2],
                "amount": args.amount,
                "other_amount_threshold": 0,
                "sqrt_price_limit": limit,
                "amount_specified_is_input": True,
                "a_to_b": not upward,
                "remaining_accounts_info": None,
            },
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
        rpc_call=default_rpc_call,
        rpc_url=url,
    )

    print("\n" + "=" * 66)
    if result.get("refused"):
        print(f"  REFUSED   {result.get('code')}")
        print(f"  {result.get('reason')}")
        if result.get("error"):
            print(f"  error: {result['error']}")
        for line in (result.get("logs") or [])[:16]:
            print(f"    {line}")
        print("=" * 66)
        print("\nDO NOT SIGN. The pre-flight did not approve this transaction.")
        return 1

    binding, strength = result.get("binding"), result.get("binding_strength")
    print(f"  SIMULATED CLEAN   {(result.get('simulation') or {}).get('compute_units')} compute units")
    print(f"  binding   {binding} [{strength}]")
    print(f"  network   {args.network}   (as you stated it, never inferred)")
    print("=" * 66)

    if not binding:
        return _stop("no binding could be computed for these bytes; the handoff cannot be checked")

    if args.send:
        return _settle(args, result["transaction_base64"], binding)

    # sign_and_send.py decodes --tx as BASE58 and nothing converts for you. The binding is
    # computed over the decoded MESSAGE, so it is the same either way — the encoding trap
    # is only in the bytes.
    tx_b58 = b58_encode(base64.b64decode(result["transaction_base64"]))
    print("\nThe blockhash expires in about a minute. Sign now or re-run — re-running is free.\n")
    print("  uv run python scripts/sign_and_send.py \\")
    print(f"    --tx {tx_b58} \\")
    print(f"    --rpc-url {url} \\")
    print(f"    --network {args.network} \\")
    print(f"    --expect-binding {binding} \\")
    print("    --send")
    print(
        "\n  --expect-binding is the only value there that did not come from --tx, so it is"
        "\n  the only one a substituted transaction cannot also satisfy."
        "\n\n  This proves the swap lands against real state with real fee accounting and a"
        "\n  mixed Token-2022/classic transfer. It does NOT prove multi-array traversal —"
        "\n  a test-sized swap never leaves its starting tick array. Prove that on a fork."
    )
    return 0


def _settle(args: argparse.Namespace, transaction_base64: str, prepared_binding: str) -> int:
    """Sign these exact bytes and broadcast, without letting them leave the process.

    WHY THIS EXISTS, AND WHAT IT TRADES. The two-terminal flow
    (``prepare`` -> copy -> ``sign_and_send --expect-binding``) has one comparison whose
    two sides have different origins, and it is the only one that can refuse a
    SUBSTITUTION: a binding written down before the bytes travelled, checked against a
    binding computed from the bytes that arrived. That check is worth having whenever
    there is a journey.

    It also has a failure mode we hit on the first real run: a blockhash lives about a
    minute, and a human copying two long base58 strings between terminals outlives it.
    The transaction was correct and the pre-flight refused it anyway, because by then it
    was stale. ``autonomous_purchase.py`` names this exact trade in its own docstring and
    resolves it the same way — one call, no window.

    So the honest accounting is NOT "we dropped a check to go faster". The carried
    binding exists to catch bytes being rewritten IN TRANSIT. Here there is no transit:
    the transaction is built, verified and signed inside one process and is never
    serialised to a terminal, a clipboard or a file. The check is not weakened; it has
    nothing left to check. What still runs, and can still refuse:

    * the fee payer must BE this keypair's pubkey — signing for an account you do not
      control produces a valid signature the network rejects, and wastes a blockhash;
    * a receipt re-taken against current state, ``replaceRecentBlockhash: false``, so a
      transaction that has gone stale or now reverts is caught rather than sent;
    * ``verify_handoff`` at ``require="exact"`` on the network YOU named — which refuses a
      stale blockhash, a reverting simulation, a message that loads accounts from a
      lookup table, and a receipt from another network.

    The keypair is read here, never printed, and never leaves this process.
    """
    from solders.keypair import Keypair
    from solders.transaction import Transaction

    if not args.keypair.exists():
        return _stop(f"no keypair at {args.keypair}")
    # `coerce_network` maps anything it does not recognise to "unknown" rather than
    # raising, so this cannot be the place a bad --network is caught. argparse `choices`
    # is, and "unknown" itself stays a legal answer that refuses at verify_handoff below.
    network = coerce_network(args.network)

    keypair = Keypair.from_bytes(bytes(json.loads(args.keypair.read_text())))
    signer = str(keypair.pubkey())
    raw = base64.b64decode(transaction_base64)
    fee_payer = str(Transaction.from_bytes(raw).message.account_keys[0])

    print("\n" + "-" * 66)
    print(f"  keypair pubkey  {signer}")
    print(f"  tx fee payer    {fee_payer}")
    if fee_payer != signer:
        return _stop(
            "this transaction pays from an account your keypair does not control.\n"
            "  Signing it would produce a signature the network rejects. Re-run with\n"
            f"  --signer {signer}, or point --keypair at the key for {fee_payer}."
        )

    receipt = simulate(
        {},
        rpc_url=args.rpc_url,
        rpc_call=default_rpc_call,
        build_call=lambda _plan: BuiltTx(tx=transaction_base64, encoding="base64"),
        replace_blockhash=False,
        network_label=f"re-simulated at signing time ({network}, read-only)",
        network=network,
        track=[signer],
    )
    print(f"  receipt         {receipt.status.upper()}", end="")
    print(f"   {receipt.units_consumed:,} CU" if receipt.units_consumed else "")
    if receipt.revert_class:
        print(f"  class           {receipt.revert_class}")

    handoff = verify_handoff(
        transaction_base64, receipt, encoding="base64", require="exact", expected_network=network
    )
    print(f"  binding         {handoff.binding}")
    print(f"  approved        {handoff.approved}: {handoff.reason}")
    if not handoff.approved or handoff.transaction_base64 is None:
        return _stop("the pre-flight did not approve these bytes. Nothing was signed.")
    if handoff.binding != prepared_binding:
        # Same process, same bytes — so this can only differ if the code between prepare
        # and here rewrote something. It is cheap, and a mismatch means a bug in US.
        return _stop(
            "the verified binding does not match the one this run just prepared.\n"
            f"  prepared  {prepared_binding}\n"
            f"  verified  {handoff.binding}\n"
            "  These bytes never left the process, so this is a defect here, not a swap."
        )

    # Sign what the gate returned, never what was parsed above: verify_handoff hands back
    # the SUBJECT it verified, and re-deriving a message from anything else reopens the
    # window from the other side.
    verified = Transaction.from_bytes(base64.b64decode(handoff.transaction_base64))
    message = verified.message
    signed = Transaction.populate(message, [keypair.sign_message(bytes(message))])

    reply = default_rpc_call(
        args.rpc_url,
        "sendTransaction",
        [
            base64.b64encode(bytes(signed)).decode(),
            {"encoding": "base64", "skipPreflight": False, "maxRetries": 3},
        ],
    )
    if "error" in reply:
        print(f"\n  SEND FAILED  {reply['error']}")
        return 1
    print(f"\n  SENT  {reply.get('result')}")
    print(f"  {network} — verify it on chain before believing this line.")
    return 0


def _stop(message: str) -> int:
    print(f"\nSTOP: {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
