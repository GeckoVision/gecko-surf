"""Plan and verify one ``let_me_buy make_purchase`` — and stop at the signature.

THE BOUNDARY IS THE POINT. This runs behind a PUBLIC, unauthenticated MCP surface
(``mcp.geckovision.tech/orquestra``), so anyone on the internet reaches it. It therefore
holds no key, touches no key, and has no path to one: it derives the accounts, refuses the
plans that are wrong before anything is built, simulates the exact bytes, and hands back
the UNSIGNED transaction with the receipt that attests it. Whoever wants it to land signs
it in their own wallet. That is also the standing repo invariant and what the public docs
claim, so a signing path here would make the docs false.

THE ORDER, AND WHY:

1. ``network`` is ASSERTED by the caller or nothing happens. Nothing infers it from the
   RPC URL — a fork proxy answers at any hostname — and ``unknown`` is a legal answer that
   refuses (:mod:`gecko.networks`).
2. The accounts are DERIVED here, offline: ``receipts`` from the seed recipe in the
   packaged config, and both token accounts from the SPL associated-token recipe. No
   hosted derive endpoint is asked, so none can be unavailable.
3. :func:`~gecko.plan_refusals.check_plan_accounts` runs on the RESOLVED map BEFORE the
   builder is called. A builder is not the authority on whether a plan is sane — the real
   one answered ``HTTP 200`` to a purchase that paid the buyer back — so judging its
   response instead would mean the bad plan had already left this machine.
4. Build once, through Orquestra ``/build``. Once, because building twice yields two
   different transactions and we would verify the one nobody signs.
5. The builder stamps a blockhash from ITS OWN RPC, and on 2026-08-12 that was ~4,500
   blocks stale — every transaction it returns is dead on arrival. So the 32 blockhash
   bytes are replaced with a fresh hash from the node we simulate against, at the offset
   the LAYOUT dictates (:func:`~gecko.txbind.blockhash_offset`), never by searching for
   the old value and never by re-assembling the message.
6. Simulate THOSE bytes with ``replaceRecentBlockhash: false``, so the binding is
   ``exact`` — it covers the blockhash the caller is about to sign over.
7. Refuse anything weaker than a passing receipt with an exact binding, and return no
   transaction when it does. A caller cannot accidentally sign a refusal.

WHO THE BUYER IS, AND WHO GETS TO SAY SO. Two configurations, one code path:

* **No account, or an account with no wallet bound** — the caller SUPPLIES the buyer.
  Correct here, and unchanged: these are bytes for somebody else's wallet to sign, so
  their owner is the only one who can name it.
* **An account with a bound wallet** (:mod:`gecko.wallet_binding`) — the buyer is LOOKED
  UP from that binding and the caller's word is not taken for it, because a caller could
  otherwise name an address they do not own. A supplied ``buyer`` that MATCHES is fine; a
  supplied ``buyer`` that differs is REFUSED rather than quietly replaced — a silent
  substitution would hand back a receipt attesting a transaction the caller did not ask
  for. A directory that cannot be read refuses too: "I could not look it up" must never
  read as "there is no binding".

That lookup is an IDENTITY record, not custody. It resolves a public key; it holds no
key and reaches no signer, so everything the first paragraph claims still holds.

WHAT A PASSING RESULT DOES NOT SAY. It does not say this is the purchase the user wanted.
It says the transaction is well-formed and would land against the state observed at that
slot. And it says so for about a minute: an exact binding dies with its blockhash, which
is deliberate — a binding that outlived its blockhash would attest a message that can no
longer land. Re-running is free.

Control plane only: the result carries the transaction and the receipt's METADATA. No
logs, no node response body, no credentials, and nothing is written anywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

# The blockhash patch lives in ONE place. A second copy of "which 32 bytes are the
# blockhash" is how a v0 message eventually gets patched at a legacy offset, so this
# imports the implementation the signing loop uses rather than restating it.
from .autonomous_purchase import (
    PurchaseRefusalCode,
    PurchaseTransportError,
    _with_fresh_blockhash,
)
from .handoff import verify_handoff
from .landing import latest_blockhash
from .netguard import UnsafeUrlError, validate_public_url
from .networks import UNKNOWN_NETWORK, Network, coerce_network
from .pda import PdaDerivationError, derive_pda
from .plan_refusals import PlanRefused, check_plan_accounts
from .provider_config import ProgramSpec, load_packaged_provider
from .rpc import RpcCall, RpcError
from .simulate import BuildCall, BuiltTx, SimulateError, simulate
from .wallet_binding import WalletBindingError, WalletDirectory

__all__ = [
    "PREPARE_PURCHASE_TOOL",
    "PrepareRefusalCode",
    "prepare_purchase_result",
    "prepare_purchase_tool",
]

#: The pair the distinctness rule is registered under. Named rather than inlined so the
#: refusal and the request can never drift onto different instructions.
LET_ME_BUY_API_ID = "let_me_buy"
MAKE_PURCHASE = "make_purchase"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

ORQUESTRA_API_BASE = "https://api.orquestra.dev/api"

#: The refusals a KEYLESS pre-flight can produce. It reuses
#: :data:`~gecko.autonomous_purchase.PurchaseRefusalCode` for the four that name the same
#: facts there ("the plan was refused", "the builder returned nothing", "the receipt did
#: not pass", "the receipt does not attest these bytes") so one word means one thing in
#: both paths, and adds the three only a public front door has. It deliberately does not
#: define its own spellings for the shared ones.
PrepareRefusalCode = (
    PurchaseRefusalCode
    | Literal[
        "network-not-asserted",
        "store-unknown",
        "argument-invalid",
        "buyer-not-bound",
        "wallet-directory-unavailable",
    ]
)


#: A store's on-chain facts that a store NAME cannot give you. ``receipts`` is derivable
#: from the name alone; who the merchant is and which mint a product is priced in are
#: written INSIDE that account, so they are read from it (:func:`resolve_store`) and never
#: guessed — a wrong authority builds a purchase that pays the wrong person.
#:
#: This was a hardcoded table of stores we had reviewed, which meant exactly one storefront
#: on letmebuy.app was reachable through chat while the program serves any number. It is
#: now filled from the chain per call. The consequence is worth stating: the merchant is
#: whoever the store's own account says it is, not someone we vouched for.
@dataclass(frozen=True)
class WiredStore:
    store_name: str
    authority: str
    mint: str
    note: str


#: The public RPC to simulate against when the caller names a network but no URL. Only a
#: PINNED constant may be defaulted here: a caller-supplied URL goes through the SSRF
#: guard first, because this surface is unauthenticated and would otherwise fetch whatever
#: address a stranger names. ``fork`` has no default — a fork is somebody's own node.
DEFAULT_RPC_URLS: dict[str, str] = {
    "mainnet": "https://api.mainnet-beta.solana.com",
    "devnet": "https://api.devnet.solana.com",
    "testnet": "https://api.testnet.solana.com",
}

#: What each account IS in this instruction, in IDL order. ``writable``/``signer`` are the
#: IDL's own flags; the phrase is what a human needs to read the plan and notice that the
#: money leaves one of these and arrives at another.
_ACCOUNT_ROLES: dict[str, tuple[str, bool, bool]] = {
    "receipts": (
        "the store's ONE account — its products, its receipt list, its authority; every "
        "instruction in the lifecycle writes this same account",
        True,
        False,
    ),
    "signer": (
        "the BUYER — pays the price and the fee, and the only signature this transaction "
        "needs",
        True,
        True,
    ),
    "authority": (
        "the merchant who owns the store. Writable but NOT a signer: the merchant does "
        "not co-sign a sale, the program checks this against receipts.authority",
        True,
        False,
    ),
    "mint": (
        "the currency the product is priced in, fixed when the merchant listed it",
        False,
        False,
    ),
    "sender_token_account": (
        "the BUYER's token account — this is the one that is DEBITED",
        True,
        False,
    ),
    "recipient_token_account": (
        "the STORE's token account — this is the one that is CREDITED",
        True,
        False,
    ),
    "token_program": ("classic SPL Token, pinned by the IDL", False, False),
    "system_program": ("the system program", False, False),
    "associated_token_program": (
        "the associated-token program, which creates a missing token account",
        False,
        False,
    ),
}

_DERIVATIONS: dict[str, str] = {
    "receipts": "PDA(['receipts', store], let_me_buy) — derived here, offline",
    "signer": "you supplied it",
    "authority": "read from the store's own account (not derivable from its name)",
    "mint": "read from the store's own account, per product",
    "sender_token_account": "ATA(buyer, token_program, mint) — derived here, offline",
    "recipient_token_account": (
        "ATA(authority, token_program, mint) — derived here, offline"
    ),
    "token_program": "pinned program id",
    "system_program": "pinned program id",
    "associated_token_program": "pinned program id",
}

#: The honesty caveat that travels with every approved result. Stated once.
_WHAT_IT_PROVES = (
    "This transaction is UNSIGNED and Gecko holds no key: you sign it, in your own "
    "wallet, and you submit it. The receipt says these exact bytes would land against "
    "the state observed at that slot — it does NOT say this is the purchase you meant. "
    "It is state-specific and it expires with its blockhash (~1 minute); after that, "
    "re-run this, which is free."
)

_MAX_STORE_BYTES = 32  # a PDA seed may not exceed 32 bytes
_MAX_PRODUCT_CHARS = 64


def _refuse(code: PrepareRefusalCode, reason: str, **extra: Any) -> dict[str, Any]:
    """A refusal, shaped so no caller can read a transaction out of it — there is none."""
    out: dict[str, Any] = {"refused": True, "code": code, "reason": reason}
    out.update(extra)
    return out


def _program_spec() -> ProgramSpec:
    _, apis = load_packaged_provider("orquestra")
    program = apis[LET_ME_BUY_API_ID].program
    if program is None:  # pragma: no cover - the packaged config always carries one
        raise PdaDerivationError("the packaged let_me_buy config carries no program")
    return program


def _pubkey_or_none(value: Any) -> str | None:
    """``value`` as a base58 pubkey, or ``None``. Untrusted public input — never trusted
    to be a string, a length, or an alphabet."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return None
    try:
        from solders.pubkey import Pubkey

        return str(Pubkey.from_string(value))
    except Exception:  # noqa: BLE001 - anything unparseable is simply not a pubkey
        return None


def _resolve_buyer(
    args: Mapping[str, Any], account: str | None, wallets: WalletDirectory | None
) -> tuple[str | None, dict[str, Any] | None]:
    """(buyer, refusal). The FIRST line: whose address may this transaction spend from?

    A bound account's buyer comes from the record, never from the request. The three
    outcomes that are not "use the caller's word" are all refusals, never substitutions:
    a mismatch, an unreadable directory, and an unparseable ``buyer``.
    """
    supplied_raw = args.get("buyer")
    supplied = _pubkey_or_none(supplied_raw)
    if supplied_raw is not None and supplied is None:
        # The caller ASSERTED something and it was not an address. That is an argument
        # error whatever the account is bound to — silently preferring the binding here
        # would build a plan the caller never described.
        return None, _refuse(
            "argument-invalid",
            "`buyer` must be a base58 Solana pubkey — the PUBLIC key of the wallet that "
            "will sign. Never a secret key, and nothing here can use one.",
        )

    binding = None
    if account and account.strip() and wallets is not None:
        try:
            binding = wallets.wallet_for(account)
        except WalletBindingError as exc:
            # Fail CLOSED. Falling through to the caller-supplied buyer would mean an
            # unreachable directory downgrades a looked-up buyer into an asserted one.
            return None, _refuse("wallet-directory-unavailable", str(exc))

    if binding is None:
        # Mode A: no account, or an account that has bound no wallet. These bytes are for
        # a wallet we know nothing about, so its owner names it.
        if supplied is None:
            return None, _refuse(
                "argument-invalid",
                "`buyer` must be a base58 Solana pubkey — the PUBLIC key of the wallet "
                "that will sign. Never a secret key, and nothing here can use one.",
            )
        return supplied, None

    if supplied is not None and supplied != binding.pubkey:
        # Names both ROLES and neither VALUE: echoing the supplied address back would
        # reflect untrusted input, and naming the bound one would make a refusal an
        # oracle for the wallet behind an account id.
        return None, _refuse(
            "buyer-not-bound",
            "the `buyer` you supplied is not the wallet bound to your account. The "
            "buyer is looked up from your session, not taken from the request, so an "
            "address you do not own cannot be named — and substituting yours silently "
            "would hand you a receipt for a transaction you did not ask for. Omit "
            "`buyer` to use your bound wallet.",
        )
    return binding.pubkey, None


def _resolve_rpc_url(raw: Any, network: Network) -> tuple[str | None, str | None]:
    """(url, refusal reason). A caller-supplied URL is guarded; a default is pinned."""
    if isinstance(raw, str) and raw.strip():
        url = raw.strip()
        try:
            # This surface is unauthenticated, so an unguarded URL here would make it a
            # proxy into whatever a stranger names — loopback, RFC1918, cloud metadata.
            validate_public_url(url)
        except UnsafeUrlError as exc:
            return None, f"rpc_url refused: {exc}"
        return url, None
    default = DEFAULT_RPC_URLS.get(network)
    if default is None:
        return None, (
            f"network={network} has no public default RPC — pass your own `rpc_url`, "
            "since a fork is somebody's own node"
        )
    return default, None


def _plan_accounts(
    store: WiredStore, buyer: str, program: ProgramSpec
) -> dict[str, str]:
    """The RESOLVED account map, derived from the packaged seed recipes. No chain read."""
    receipts = derive_pda(program.pdas["receipts"], {"store_name": store.store_name})
    sender = derive_pda(
        program.pdas["sender_token_account"],
        {"signer": buyer, "token_program": TOKEN_PROGRAM, "mint": store.mint},
    )
    recipient = derive_pda(
        program.pdas["recipient_token_account"],
        {
            "authority": store.authority,
            "token_program": TOKEN_PROGRAM,
            "mint": store.mint,
        },
    )
    return {
        "receipts": receipts.address,
        "signer": buyer,
        "authority": store.authority,
        "mint": store.mint,
        "sender_token_account": sender.address,
        "recipient_token_account": recipient.address,
        "token_program": TOKEN_PROGRAM,
        "system_program": SYSTEM_PROGRAM,
        "associated_token_program": ATA_PROGRAM,
    }


def _account_plan(
    accounts: Mapping[str, str], program: ProgramSpec
) -> list[dict[str, Any]]:
    """The account map as an ORDERED, self-describing plan: what each account is, whether
    it is written, whether it signs, how it was obtained, and at which provenance tier."""
    origins = program.pda_origins
    plan: list[dict[str, Any]] = []
    for name, address in accounts.items():
        role, writable, signs = _ACCOUNT_ROLES[name]
        entry: dict[str, Any] = {
            "account": name,
            "address": address,
            "role": role,
            "writable": writable,
            "signer": signs,
            "derivation": _DERIVATIONS[name],
        }
        if name in origins:
            # A PRESENT entry always says something, and `None` says the loudest thing:
            # this account carries no tier we established (an origin outside the closed
            # ladder, or a config we did not author — see `ConfigTrust`). Omitting the
            # field there would render "we have not verified this" as "nothing to
            # report", which is the same fail-open the trust mode exists to close.
            # ABSENT stays absent: an account the config never mentions is untouched.
            entry["provenance"] = origins[name] or "flagged"
        plan.append(entry)
    return plan


def prepare_purchase_result(
    arguments: Mapping[str, Any],
    *,
    build_call: BuildCall | None = None,
    rpc_call: RpcCall | None = None,
    account: str | None = None,
    wallets: WalletDirectory | None = None,
) -> dict[str, Any]:
    """Plan, verify, and hand back the UNSIGNED transaction. Never raises for an answer.

    An expected refusal — an unasserted network, a store that does not resolve, a plan that pays the
    buyer back, a simulation that reverts — comes back as a structured refusal carrying no
    transaction, because those are answers a caller must handle. A transport failure comes
    back as ``{"error": ...}`` in the surface's own style, redacted to the failure class.

    ``account`` is the AUTHENTICATED caller's stable id (:class:`gecko.keyauth.AuthDecision`
    ``.account``) — resolved by the transport, never read from ``arguments``, since an
    argument is just the caller's word. With ``wallets``, it decides the buyer; without
    either, the caller-supplied ``buyer`` is used exactly as before.

    ``build_call``/``rpc_call``/``wallets`` are injected seams: the whole path is
    falsifiable offline.
    """
    args = arguments or {}

    # 1. THE NETWORK, asserted. Before any transport, so a caller who never said cannot
    #    make this machine fetch anything.
    network = coerce_network(args.get("network"))
    if network == UNKNOWN_NETWORK:
        return _refuse(
            "network-not-asserted",
            "say which network this is for: mainnet, devnet, testnet or fork. Nothing "
            "here infers it — a fork proxy answers at any hostname, so an RPC URL is "
            "evidence of nothing, and 'unknown' approves nothing.",
            network=UNKNOWN_NETWORK,
        )

    store_name = args.get("store")
    if not isinstance(store_name, str) or not store_name.strip():
        return _refuse("argument-invalid", "`store` is required (the store's name)")
    store_name = store_name.strip()
    if len(store_name.encode("utf-8")) > _MAX_STORE_BYTES:
        return _refuse(
            "argument-invalid",
            f"`store` is longer than {_MAX_STORE_BYTES} bytes and cannot be a PDA seed",
        )
    product = args.get("product")
    if not isinstance(product, str) or not product.strip():
        return _refuse("argument-invalid", "`product` is required (the product's name)")
    product = product.strip()[:_MAX_PRODUCT_CHARS]

    buyer, buyer_refusal = _resolve_buyer(args, account, wallets)
    if buyer is None:
        return buyer_refusal or _refuse("argument-invalid", "no usable `buyer`")

    table = args.get("table", 0)
    if isinstance(table, bool) or not isinstance(table, int) or not 0 <= table <= 255:
        return _refuse(
            "argument-invalid", "`table` must be a whole number from 0 to 255 (u8)"
        )

    rpc_url, url_refusal = _resolve_rpc_url(args.get("rpc_url"), network)
    if rpc_url is None:
        return _refuse(
            "argument-invalid", url_refusal or "no usable rpc_url", network=network
        )

    # WHO the merchant is and WHICH mint a product is priced in are written INSIDE the
    # store's own account, so they are READ, never guessed and never shipped as a
    # constant. This used to be a hardcoded table, which meant exactly one storefront on
    # letmebuy.app was reachable through chat while the program serves any number.
    #
    # One targeted `getAccountInfo` on the address the NAME derives — not a program scan,
    # so it works on nodes with `getProgramAccounts` disabled and the address asked for is
    # the address the name entails. Every failure is a refusal that names what it looked
    # at: absence never falls back to a store the caller did not ask for.
    #
    # Deliberately AFTER the rpc_url guard (it needs a node) and BEFORE the builder, so an
    # unresolvable store costs no build and no simulation.
    # Lazy: `store_accounts` imports this module's program spec, so a top-level import
    # is a cycle. Same pattern `find_start` uses for its provider_config reads.
    from .store_accounts import ProductNotOnMenu, StoreResolutionError, resolve_store

    try:
        resolved = resolve_store(store_name, rpc_url=rpc_url, rpc_call=rpc_call)
        purchase = resolved.accounts_for(product)
    except ProductNotOnMenu as exc:
        return _refuse("store-unknown", str(exc), network=network)
    except StoreResolutionError as exc:
        # Covers StoreNotFound and StoreAccountsMismatch — the account is missing, owned
        # by another program, undecodable, or calls itself a different name than the one
        # it was derived from. All four mean the same thing to a buyer: there is no
        # storefront here, and nothing substitutes one.
        return _refuse("store-unknown", str(exc), network=network)
    except RpcError as exc:
        # Same shape the other transport failures use below: the failure CLASS and its
        # message, never the node's response body. A node we cannot reach is not a
        # refusal ABOUT the store — it is us having no answer, and those read differently.
        return {"error": f"{type(exc).__name__}: {exc}"}
    store = WiredStore(
        store_name=resolved.store_name,
        authority=resolved.authority,
        mint=purchase.product.mint,
        note=(
            f"read from the store's own account at {resolved.receipts} — "
            f"{purchase.product.name} at {purchase.product.price_ui} "
            f"({purchase.product.price_raw} raw, {purchase.product.decimals} decimals)"
        ),
    )

    try:
        return _prepare(
            store=store,
            product=product,
            buyer=buyer,
            table=table,
            network=network,
            rpc_url=rpc_url,
            build_call=build_call,
            rpc_call=rpc_call,
        )
    except (
        PdaDerivationError,
        PurchaseTransportError,
        RpcError,
        SimulateError,
    ) as exc:
        # The failure CLASS and its message, which these types keep free of request
        # bodies and credentials by construction. Never the node's response.
        return {"error": f"{type(exc).__name__}: {exc}"}


def _prepare(
    *,
    store: WiredStore,
    product: str,
    buyer: str,
    table: int,
    network: Network,
    rpc_url: str,
    build_call: BuildCall | None,
    rpc_call: RpcCall | None,
) -> dict[str, Any]:
    """Everything from the derived plan to the verified, unsigned bytes."""
    program = _program_spec()
    accounts = _plan_accounts(store, buyer, program)
    instruction_args = {
        "store_name": store.store_name,
        "product_name": product,
        "table_number": table,
    }

    # 2. THE PLAN, before the builder is asked.
    try:
        check_plan_accounts(LET_ME_BUY_API_ID, MAKE_PURCHASE, accounts)
    except PlanRefused as refusal:
        return _refuse(
            "plan-refused",
            f"[{refusal.code}] {refusal.reason}",
            network=network,
            refuted_by=refusal.refuted_by,
            accounts=_account_plan(accounts, program),
        )

    # 3. BUILD, exactly once.
    build_url = (
        f"{ORQUESTRA_API_BASE}/{program.orquestra_project}"
        f"/instructions/{MAKE_PURCHASE}/build"
    )
    builder = build_call or _http_build_call
    built = builder(
        {
            "build_url": build_url,
            "accounts": dict(accounts),
            "args": dict(instruction_args),
            "feePayer": buyer,
        }
    )
    if not isinstance(built, BuiltTx) or not built.tx:
        return _refuse(
            "build-returned-nothing",
            "the builder returned no transaction, so there is nothing to verify",
            network=network,
        )

    # 4. A FRESH BLOCKHASH, written into the built bytes at the offset the layout gives.
    #    The builder stamps one from its own RPC and it has been observed ~4,500 blocks
    #    stale, which is dead on arrival; patching 32 bytes leaves every account and every
    #    byte of instruction data exactly as the builder emitted them.
    blockhash, last_valid_block_height = latest_blockhash(rpc_url, rpc_call)
    subject = _with_fresh_blockhash(built, blockhash)

    # 5. SIMULATE THE SUBJECT — the exact bytes handed back, nothing re-assembled.
    receipt = simulate(
        {},
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        build_call=lambda _plan: BuiltTx(tx=subject, encoding="base64"),
        replace_blockhash=False,
        network_label=f"simulated against {network} (read-only, unsigned)",
        network=network,
    )
    if receipt.status != "pass":
        return _refuse(
            "receipt-failed",
            "the simulation did not pass, so no transaction is returned: a transaction "
            "that reverts against observed state would only cost you a fee on chain",
            network=network,
            status=receipt.status,
            revert_class=receipt.revert_class,
            units_consumed=receipt.units_consumed,
            accounts=_account_plan(accounts, program),
        )

    # 6. THE BINDING, at `exact`. A structural binding is blockhash-blind, so accepting
    #    one here would hand back a message carrying a blockhash nobody simulated.
    handoff = verify_handoff(
        subject, receipt, require="exact", expected_network=network, encoding="base64"
    )
    if not handoff.approved or handoff.transaction_base64 is None:
        return _refuse(
            "binding-refused",
            f"the receipt does not attest these exact bytes: {handoff.reason}",
            network=network,
            status=receipt.status,
            units_consumed=receipt.units_consumed,
        )

    return {
        "refused": False,
        "status": receipt.status,
        "units_consumed": receipt.units_consumed,
        "binding": receipt.message_binding,
        "binding_strength": receipt.binding_strength,
        "network": receipt.network,
        "network_label": receipt.network_label,
        "instruction": {
            "api_id": LET_ME_BUY_API_ID,
            "instruction": MAKE_PURCHASE,
            "program_id": program.program_id,
            "built_by": build_url,
            "store_note": store.note,
        },
        "args": instruction_args,
        "fee_payer": buyer,
        "accounts": _account_plan(accounts, program),
        "transaction": {
            "signed": False,
            "encoding": "base64",
            "unsigned_transaction": handoff.transaction_base64,
            "who_signs": "you do, in your own wallet — Gecko holds no key",
        },
        # WHERE these bytes can be sent. Everything else in this result names what the
        # transaction IS; without this the one node that can accept it is missing from
        # the answer — and on a fork that node is the caller's own, appearing nowhere else.
        "submit": {"rpc_url": rpc_url},
        "next_step": _next_step(
            handoff.transaction_base64, receipt.message_binding, rpc_url
        ),
        "expires": {
            "blockhash": blockhash,
            "last_valid_block_height": last_valid_block_height,
            "note": (
                "these bytes stop being landable after that block height — about 40 "
                "seconds, not the full minute the 150-slot budget suggests, because the "
                "blockhash is read at `finalized` commitment and is already ~30 slots "
                "behind when you receive it. The receipt expires with them; re-running "
                "is free, so prepare immediately before signing"
            ),
        },
        "what_this_proves": _WHAT_IT_PROVES,
    }


#: Signers verified to take this exact primitive — base64 in, signed base64 out — with
#: which client each one reaches and HOW TO ADD IT. EXAMPLES, never the contract: naming
#: one signer's tool in the result would turn a keyless handoff into an integration, and a
#: fourth signer would then need a change from us. Research:
#: docs/specs/2026-08-14-who-signs.md.
#:
#: WHY THE INSTALL LINE IS HERE. A caller with no signer mounted cannot act on "PayBox
#: reaches Claude web" — it names a product, not a next move — so the loop stalls at the
#: one step we do not perform. Carrying the connector means the agent can say exactly what
#: to add AT THE MOMENT a signature is needed, rather than the user having to know in
#: advance that two connectors are involved.
#:
#: AND WHY WE DO NOT BROKER IT INSTEAD. Signing in on the user's behalf would mean holding
#: an access token that authorises spending from their wallet — a path to a key, which
#: this module's first paragraph and the public docs both say does not exist. The friction
#: is a discovery problem and gets a discovery fix; it is not a reason to take custody.
_SIGNERS_KNOWN_TO_WORK: tuple[dict[str, str], ...] = (
    {
        "name": "PayBox",
        "call": 'request_wallet_sign, op="solanaTransaction", transactionBase64',
        "reaches": "Claude web, ChatGPT, Gemini, Grok — hosted MCP connector",
        "connector": "https://api.paybox.sh/mcp",
        "how_to_add": (
            "Settings -> Connectors -> add a custom connector with that URL, then "
            "Connect and sign in. No API key to copy; it is an OAuth flow."
        ),
    },
    {
        "name": "Phantom MCP",
        "call": "signTransaction",
        "reaches": "Claude Desktop, Cursor, Claude Code — not Claude web",
        "connector": "npx @phantom/mcp-server",
        "how_to_add": (
            "a LOCAL server: add it to your client's MCP config. Being local is why it "
            "does not reach Claude web, and why your key never leaves your machine."
        ),
    },
    {
        "name": "Privy agent-wallet CLI",
        "call": 'signTransaction, {"transaction": "<base64>"}',
        "reaches": "anywhere a shell runs",
        "connector": "pnpm --package=@privy-io/agent-wallet-cli dlx privy-agent-wallet",
        "how_to_add": "run its `login` on your own machine, then call `signTransaction`.",
    },
)


def _next_step(
    transaction_base64: str | None, binding: str | None, rpc_url: str
) -> dict[str, Any]:
    """The machine-readable affordance: sign, verify, submit — in that order.

    The result already carried everything needed to PRODUCE a signature and nothing an
    agent could act on: ``who_signs`` was prose. This is the same fact in a shape a tool
    call can consume.

    VERIFY SITS BETWEEN SIGN AND SUBMIT, and that is the point of the ordering. Between
    handing over bytes and getting a signature back, nothing of ours runs — the bytes
    could have been swapped for others that sign just as cleanly, and no custody backend
    will notice, because custody protects the key and never the action. Verifying after
    broadcast is a post-mortem; verifying before it is a decision.
    """
    return {
        "note": (
            "these bytes are UNSIGNED. Sign them wherever your key lives, check the "
            "signature is over the transaction that was actually verified, and only then "
            "send it."
        ),
        "do": [
            {
                "step": "sign",
                "what": (
                    "hand this base64 to your signer and take back the signed base64. "
                    "Any signer that signs raw bytes works — deliberately no tool is "
                    "named here, because the primitive is the contract, not the vendor"
                ),
                "encoding": "base64",
                "transaction": transaction_base64,
                "signers_known_to_work": [dict(s) for s in _SIGNERS_KNOWN_TO_WORK],
            },
            {
                "step": "verify",
                "what": (
                    "prove the signed bytes are the ones this receipt attested, BEFORE "
                    "you send them — a signer will happily sign a substituted "
                    "transaction, and this is where that becomes visible while it is "
                    "still free"
                ),
                "tool": "verify_signed_transaction",
                "binding": binding,
            },
            {
                "step": "submit",
                "what": (
                    "send the signed transaction to this node — the same one the "
                    "simulation ran against, so the state it was checked on is the state "
                    "it lands on"
                ),
                "rpc_url": rpc_url,
                "method": "sendTransaction",
            },
        ],
    }


def _http_build_call(plan: Mapping[str, Any]) -> BuiltTx:
    """The default builder: Orquestra ``/build`` through the engine's own POST seam.

    Wrapped rather than passed directly so the private default stays referenced in exactly
    one place, and so the URL it is given is this module's constant — never a caller's.
    """
    from .simulate import _default_build_call

    return _default_build_call(plan)


#: What ``buyer`` means when the caller is the only one who can name it (mode A).
_BUYER_SUPPLIED = (
    "the PUBLIC key of the wallet that will sign and pay. Never a secret key — nothing "
    "here can use one."
)

#: ...and what it means when this deployment can look the caller's wallet up. The schema
#: states the rule the server enforces: an agent should not have to be REFUSED to learn it.
_BUYER_BOUND = (
    "OPTIONAL here: if your account has a wallet bound to it, omit this and that wallet "
    "is used — the buyer is looked up from your session, not taken from your word. If "
    "you do supply it, it must MATCH your bound wallet or the call is REFUSED (it is "
    "never silently replaced). With no bound wallet this is required, and is the PUBLIC "
    "key of the wallet that will sign and pay. Never a secret key — nothing here can "
    "use one."
)


PREPARE_PURCHASE_TOOL: dict[str, Any] = {
    "name": "prepare_purchase",
    "description": (
        "Plan and verify a real purchase from a let_me_buy storefront, and hand back the "
        "UNSIGNED transaction. Gecko holds no key: YOU SIGN it in your own wallet and you "
        "submit it — nothing here signs, broadcasts, or touches a key. It derives every "
        "account offline (the store's receipts PDA from its seed recipe, both token "
        "accounts from the SPL associated-token recipe), refuses a plan that would pay "
        "the buyer back BEFORE the builder is asked, replaces the builder's stale "
        "blockhash with a fresh one from the node it simulates against, and simulates "
        "the exact bytes it returns so the receipt binds them exactly. Returns the "
        "receipt (status, compute units, revert class), the binding, the resolved "
        "accounts with each one's role, and the transaction; a simulation that fails "
        "returns the refusal and NO transaction. "
        "WHEN TO CALL THIS: **after** the buyer has chosen, immediately before signing — "
        "never while they are still deciding. The bytes it returns carry a live blockhash "
        "and stop being landable roughly 40 seconds later, so preparing several products "
        "to compare them, or preparing before a human approves, spends the whole window on "
        "transactions nobody signs. Browse with `list_stores` instead: it costs nothing, "
        "expires never, and carries every price. Re-running this is free, so prepare LATE "
        "rather than early. ONE PRODUCT PER CALL: the program takes a single product name "
        "and no quantity, so several items means several purchases, each with its own "
        "receipt and its own signature. "
        "HONEST LIMITS: the receipt is state-specific and EXPIRES with its blockhash "
        "(~40 seconds, not the full minute the slot budget suggests — the blockhash is "
        "read at `finalized`, which is already ~30 slots behind), and a passing receipt is "
        "not a promise the transaction is what you wanted. It says only that these bytes "
        "are WELL-FORMED and would land against the state observed at that slot. Read the "
        "account plan before you sign."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "store": {
                "type": "string",
                "description": (
                    "the storefront's name, exactly as it appears on chain — call "
                    "`list_stores` to see what exists. The merchant and the price are "
                    "read from that store's own account"
                ),
            },
            "product": {"type": "string", "description": "the product's name"},
            "buyer": {"type": "string", "description": _BUYER_SUPPLIED},
            "network": {
                "type": "string",
                "enum": ["mainnet", "devnet", "testnet", "fork"],
                "description": (
                    "which network this is for. YOU say; nothing here infers it from the "
                    "RPC URL, and there is no default."
                ),
            },
            "table": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "description": "the table number the order is for (u8)",
            },
            "rpc_url": {
                "type": "string",
                "description": (
                    "the http(s) RPC to simulate against; defaults to the public endpoint "
                    "for the network you named. Required for a fork."
                ),
            },
        },
        "required": ["store", "product", "buyer", "network"],
        "additionalProperties": False,
    },
}


def prepare_purchase_tool(*, buyer_bound: bool = False) -> dict[str, Any]:
    """The tool definition, honest about which buyer rule THIS deployment enforces.

    ``buyer_bound`` is set by the surface when a wallet directory is wired. It changes
    nothing about what the tool DOES — it changes what the schema admits, so an agent
    reads the rule rather than discovering it by being refused.

    Returns a fresh dict every call: a caller that mutated a shared constant would edit
    the schema every other surface serves.
    """
    tool: dict[str, Any] = deepcopy(PREPARE_PURCHASE_TOOL)
    if not buyer_bound:
        return tool
    schema: dict[str, Any] = tool["inputSchema"]
    schema["properties"]["buyer"]["description"] = _BUYER_BOUND
    schema["required"] = [name for name in schema["required"] if name != "buyer"]
    return tool
