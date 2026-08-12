"""Run ONE autonomous purchase: prepare, verify, authorise, sign and settle in one call.

    uv run python scripts/autonomous_purchase.py --network fork \
        --rpc-url http://127.0.0.1:8999 --keypair ~/.gecko/wallets/txodds-subscriber.json

    uv run python scripts/autonomous_purchase.py --network fork \
        --rpc-url http://127.0.0.1:8999 --privy      # key stays in Privy's enclave

TWO KEY HOLDERS, NEVER BOTH AND NEVER NEITHER. ``--keypair`` reads a file on this machine;
``--privy`` asks a Privy server wallet to sign, so no key exists here at all
(``scripts/privy_backend.py``). Naming both, or naming neither, stops the run — a signer
that picks its own key source when the caller was ambiguous is the fall-back the seam
exists to forbid.

THIS SCRIPT IS THIN ON PURPOSE. It parses flags, wires a signing backend, calls
:func:`gecko.autonomous_purchase.run_purchase`, and prints. Every decision — the plan
refusal, the binding, the spend policy, the signature — is made inside the package. If a
rule ever appears in this file, it belongs in ``gecko/`` instead.

WHY THERE IS NO ``--send`` FLAG, and why that is not a missing safety rail. This loop signs
and broadcasts in one call by construction: a blockhash lives about a minute, and a
confirmation prompt between verification and signing outlives it. The rail is
``--network``, which has no default and must be a network you named, plus the policy caps
you configured before the run. To not spend money, do not point it at mainnet.

MAINNET IS THE FOUNDER'S CALL, NEVER AN AGENT'S. This script is developed and proven
against a surfpool fork, where a purchase costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.autonomous_purchase import (  # noqa: E402
    PurchaseRefused,
    PurchaseSettled,
    PurchasePlan,
    default_spend_policy,
    run_purchase,
)
from gecko.networks import NETWORKS, coerce_network  # noqa: E402
from gecko.rpc import default_rpc_call, user_agent  # noqa: E402
from gecko.signer import (  # noqa: E402
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SigningAttestation,
    SigningBackend,
    TransactionSigner,
)
from gecko.simulate import BuiltTx  # noqa: E402
from gecko.spend_policy import (  # noqa: E402
    AdvisorySpendLedger,
    FileSpendLedger,
    InMemorySpendLedger,
    SpendPolicyGate,
)
from scripts.privy_backend import PrivyBackendError  # noqa: E402
from scripts.privy_backend import from_env as privy_from_env  # noqa: E402

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

BUILD_URL = "https://api.orquestra.dev/api/p7o7nf4pucllzadrmiqhf/instructions/make_purchase/build"
STORE_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"
STORE_AUTHORITY = "8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V"
STORE_TOKEN_ACCOUNT = "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY"


class LocalKeypairBackend:
    """The key holder, OUTSIDE ``gecko/``. Reads one file, signs one message, prints nothing.

    The material never crosses into the package: the signer asks for a pubkey and a
    signature, and there is no member through which a secret could travel back.
    """

    def __init__(self, path: Path) -> None:
        from solders.keypair import Keypair

        self._keypair = Keypair.from_bytes(bytes(json.loads(path.read_text())))

    @property
    def pubkey(self) -> str:
        return str(self._keypair.pubkey())

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        from solders.transaction import Transaction

        transaction = Transaction.from_bytes(unsigned_transaction)
        message = transaction.message
        signature = self._keypair.sign_message(bytes(message))
        return bytes(Transaction.populate(message, [signature]))


def derive_ata(owner: str, mint: str) -> str:
    """The associated token account — a PDA, so it is derived, never guessed."""
    from solders.pubkey import Pubkey

    address, _bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(owner)),
            bytes(Pubkey.from_string(TOKEN_PROGRAM)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ATA_PROGRAM),
    )
    return str(address)


def http_build_call(request: object) -> BuiltTx:
    """Ask the real builder. Treats the response as untrusted — shape checked, never trusted."""
    import urllib.request

    body = json.dumps(request).encode("utf-8")
    http = urllib.request.Request(
        BUILD_URL,
        data=body,
        headers={"content-type": "application/json", "user-agent": user_agent()},
        method="POST",
    )
    with urllib.request.urlopen(http, timeout=45) as response:  # noqa: S310 - pinned https
        built = json.loads(response.read())
    serialized = built.get("serializedTransaction")
    if not isinstance(serialized, str):
        return BuiltTx(tx="", encoding="base58")
    encoding = built.get("encoding")
    return BuiltTx(
        tx=serialized, encoding=encoding if isinstance(encoding, str) else "base58"
    )


#: Where a rolling cap remembers itself when nobody says otherwise. Under the user's own
#: config directory rather than a temp path, because a budget that a reboot clears is not
#: a budget.
DEFAULT_LEDGER_PATH = str(Path.home() / ".gecko" / "spend-ledger.jsonl")

#: The one value that opts OUT of durability. Spelled like a sentinel so it cannot be
#: reached by a typo in a path.
EPHEMERAL_LEDGER = ":memory:"


def build_ledger(path: str | None) -> AdvisorySpendLedger:
    """The ledger for this run. Durable unless the caller explicitly asks otherwise.

    The polarity is the point. A rolling cap kept in memory is not a cap — restart the
    process and the day's budget is new, which on a rolling deploy means the daily cap
    resets every time the task is replaced. So durability is what you get by saying
    nothing, and the ephemeral ledger has to be named.

    ONE HONEST LIMIT: this is a FILE. It survives a process restart, and on a host with
    persistent storage it survives a reboot. On ECS Fargate the task's disk does NOT
    survive task REPLACEMENT, so a rolling deploy still starts a fresh window unless this
    path points at persistent storage (EFS) or the ledger is moved to a shared store. The
    file form fixes the crash-loop; it does not by itself fix the deploy.
    """
    if path == EPHEMERAL_LEDGER:
        return InMemorySpendLedger()
    return FileSpendLedger(path=path or DEFAULT_LEDGER_PATH)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument(
        "--network",
        required=True,
        choices=sorted(NETWORKS),
        help=(
            "Which network --rpc-url points at. YOU say; nothing here guesses. A fork "
            "proxy answers at any hostname, so the URL is evidence of nothing."
        ),
    )
    parser.add_argument(
        "--keypair",
        type=Path,
        default=None,
        help="Sign locally with this keypair file. Mutually exclusive with --privy.",
    )
    parser.add_argument(
        "--privy",
        action="store_true",
        help=(
            "Sign with a Privy server wallet instead of a local file — the key stays in "
            "Privy's enclave and never exists here. Reads PRIVY_APP_ID, PRIVY_APP_SECRET "
            "and PRIVY_WALLET_ID from the environment; the wallet's address is fetched "
            "from Privy, never asserted. Privy is asked to SIGN, never to broadcast."
        ),
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help=(
            "Where the rolling spend windows are remembered. Defaults to "
            f"{DEFAULT_LEDGER_PATH} so a restart does not hand the agent a fresh daily "
            f"cap. Pass {EPHEMERAL_LEDGER!r} for an in-process ledger that forgets — "
            "fine for a one-shot run, never for anything long-lived."
        ),
    )
    parser.add_argument("--store", default="jonasbar")
    parser.add_argument("--product", default="Water")
    parser.add_argument("--table", type=int, default=11)
    parser.add_argument(
        "--max-usdc-raw",
        type=int,
        default=1_000_000,
        help=(
            "Per-transaction USDC ceiling in RAW base units at 6 decimals "
            "(1000000 = 1.00 USDC). Set it below the price to watch the gate refuse."
        ),
    )
    parser.add_argument(
        "--self-transfer",
        action="store_true",
        help=(
            "Bind the buyer's own token account as the RECIPIENT — a purchase that pays "
            "the buyer back. Demonstrates the plan refusal that fires before the builder "
            "is ever asked."
        ),
    )
    args = parser.parse_args(argv)
    network = coerce_network(args.network)

    # WHICH key holder. Exactly one, named explicitly: a run that could fall back from one
    # signer to another is the fall-back the seam exists to forbid.
    backend: SigningBackend
    if args.privy:
        if args.keypair is not None:
            print("STOP: pass --privy or --keypair, never both")
            return 2
        try:
            backend = privy_from_env(network=args.network)
        except PrivyBackendError as exc:
            print(f"STOP: {exc}")
            return 2
        profile_name = EXTERNAL_SIGNER_PROFILE_NAME
    else:
        if args.keypair is None:
            print(
                "STOP: pass --keypair <path> (or --privy to sign with a Privy wallet)"
            )
            return 2
        if not args.keypair.exists():
            print(f"STOP: no keypair at {args.keypair}")
            return 2
        backend = LocalKeypairBackend(args.keypair)
        profile_name = DEVELOPER_KEYPAIR_FILE_PROFILE_NAME
    buyer = backend.pubkey
    buyer_ata = derive_ata(buyer, USDC)
    recipient = buyer_ata if args.self_transfer else STORE_TOKEN_ACCOUNT

    # The policy is authored HERE, before the run, by a human editing these numbers.
    gate = SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=frozenset(
                {STORE_RECEIPTS, STORE_AUTHORITY, STORE_TOKEN_ACCOUNT, buyer_ata}
            ),
            usdc_per_transaction_raw=args.max_usdc_raw,
        ),
        ledger=build_ledger(args.ledger),
    )
    signer = TransactionSigner(
        backend=backend,
        # The profile NAME is vocabulary, not a label: it travels to the backend inside
        # SigningAttestation.profile, which is what an external signer keys its own policy
        # on. This used to read "local", a member of no vocabulary at all.
        profile=SignerProfile(name=profile_name, network=network, authorized=True),
        spend_gate=gate,
    )
    plan = PurchasePlan(
        api_id="let_me_buy",
        instruction="make_purchase",
        accounts={
            "receipts": STORE_RECEIPTS,
            "signer": buyer,
            "authority": STORE_AUTHORITY,
            "mint": USDC,
            "sender_token_account": buyer_ata,
            "recipient_token_account": recipient,
            "token_program": TOKEN_PROGRAM,
            "system_program": SYSTEM_PROGRAM,
            "associated_token_program": ATA_PROGRAM,
        },
        args={
            "store_name": args.store,
            "product_name": args.product,
            "table_number": args.table,
        },
        fee_payer=buyer,
    )

    print(f"  buyer        {buyer}")
    print(f"  USDC account {buyer_ata}")
    print(f"  network      {network}   (as you stated it, never inferred)")
    print(f"  USDC cap     {args.max_usdc_raw} raw (6 decimals)")

    outcome = run_purchase(
        network=network,
        rpc_url=args.rpc_url,
        plan=plan,
        signer=signer,
        spend_gate=gate,
        build_call=http_build_call,
        rpc_call=default_rpc_call,
    )

    print("\n" + "=" * 66)
    if isinstance(outcome, PurchaseRefused):
        print(f"  REFUSED   [{outcome.code}]")
        print(f"  reason    {outcome.reason}")
        if outcome.receipt is not None:
            print(f"  receipt   {outcome.receipt.status}  {outcome.predicted_units} CU")
            print(f"  binding   {outcome.receipt.binding_strength}")
        if outcome.verdict is not None:
            print(f"  verdict   {outcome.verdict.code}")
        print("=" * 66)
        print("\n  Nothing was signed and nothing was sent.")
        return 1

    assert isinstance(outcome, PurchaseSettled)
    print("  SETTLED")
    print(f"  signature  {outcome.signature}")
    print(f"  network    {outcome.network}")
    print(f"  verdict    authorized — {outcome.verdict.reason}")
    print(f"  predicted  {outcome.predicted_units} CU (simulation)")
    print(f"  consumed   {outcome.consumed_units} CU (chain)")
    print(
        f"  blockhash  {outcome.blockhash} (valid to {outcome.last_valid_block_height})"
    )
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
