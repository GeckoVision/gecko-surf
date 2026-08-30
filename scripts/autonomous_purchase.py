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
    PurchaseTransportError,
    default_spend_policy,
    run_purchase,
)
from gecko.mainnet_ledger import LedgerRow  # noqa: E402
from gecko.mainnet_ledger import record as record_ledger  # noqa: E402
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
from gecko.store_accounts import (  # noqa: E402
    StoreResolutionError,
    allowed_destinations,
    TOKEN_PROGRAM_ID,
    derive_ata,
    purchase_accounts,
    purchase_args,
    resolve_store,
)
from scripts.privy_backend import PrivyBackendError  # noqa: E402
from scripts.privy_backend import from_env as privy_from_env  # noqa: E402

BUILD_URL = "https://api.orquestra.dev/api/p7o7nf4pucllzadrmiqhf/instructions/make_purchase/build"


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
    parser.add_argument(
        "--store",
        default="jonasbar",
        help=(
            "which storefront to buy from, by name. It selects the ACCOUNTS too — the "
            "receipts PDA, the merchant authority and the token account that gets "
            "credited are read from that store's own on-chain account, not from a "
            "constant. A store nobody deployed refuses here."
        ),
    )
    parser.add_argument(
        "--product",
        default="Water",
        help="the product to buy. It must be on that store's menu, or the run stops.",
    )
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

    # WHICH store, resolved from the name BEFORE anything is built. The store's accounts,
    # the instruction's `store_name` and the spend policy's destinations all come from this
    # one object, so `--store` cannot select some of them and leave the rest pointing at
    # another merchant. A name nobody deployed, or a product nobody listed, stops here.
    try:
        store = resolve_store(
            args.store, rpc_url=args.rpc_url, rpc_call=default_rpc_call
        ).accounts_for(args.product)
    except StoreResolutionError as exc:
        print(f"STOP: {exc}")
        return 2
    buyer_ata = derive_ata(buyer, store.mint, token_program=TOKEN_PROGRAM_ID)
    recipient = buyer_ata if args.self_transfer else store.token_account

    # The policy is authored HERE, before the run, by a human editing these numbers — but
    # WHO may be paid is derived from the resolved store, never retyped.
    gate = SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=allowed_destinations(store, buyer_ata=buyer_ata),
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
        accounts=purchase_accounts(store, buyer=buyer, recipient=recipient),
        args=purchase_args(store, table=args.table),
        fee_payer=buyer,
    )

    print(f"  buyer        {buyer}")
    print(f"  token acct   {buyer_ata}")
    print(f"  store        {store.store_name}")
    print(f"  receipts     {store.receipts}   (derived from the name, read from chain)")
    print(f"  paying       {store.token_account}   (authority {store.authority})")
    print(f"  product      {store.product.name} at {store.product.price_ui}")
    print(f"  network      {network}   (as you stated it, never inferred)")
    print(f"  USDC cap     {args.max_usdc_raw} raw (6 decimals)")

    try:
        outcome = run_purchase(
            network=network,
            rpc_url=args.rpc_url,
            plan=plan,
            signer=signer,
            spend_gate=gate,
            build_call=http_build_call,
            rpc_call=default_rpc_call,
        )
    except PurchaseTransportError as exc:
        # The package attaches the SIGNATURE to this error precisely so a broadcast
        # whose confirmation timed out can be looked up. Letting it surface as a raw
        # traceback throws that away and turns "look it up rather than assuming either
        # outcome" into advice the reader cannot follow — a refusal you cannot act on
        # is a shrug. Print it, and say what to do with it.
        print("\n" + "=" * 66)
        print("  UNCONFIRMED — the transaction was broadcast; its outcome is UNKNOWN")
        print(f"  reason     {exc}")
        signature = getattr(exc, "signature", None)
        if signature:
            print(f"  signature  {signature}")
            print(f"  look it up  https://solscan.io/tx/{signature}")
            print(
                "  Do NOT re-run until you have: a broadcast that lands late still "
                "spends, and a second run would pay twice."
            )
        else:
            print("  no signature was attached — check the payer's recent signatures")
        print("=" * 66)
        return 2

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
    # Both halves have been printed two lines apart for months and never written down
    # together. Mainnet only; a fork signature in the mainnet ledger would be the
    # fork-is-not-mainnet error inside the file whose job is being trustworthy.
    written = record_ledger(
        LedgerRow(
            signature=str(outcome.signature),
            predicted_cu=outcome.predicted_units,
            predicted_source="scripts/autonomous_purchase.py (receipt, pre-signature)",
            network=str(outcome.network),
            program="let_me_buy",
        )
    )
    if written is not None:
        print(f"  logged     {written}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
