"""``try_purchase`` — the rehearsal, as a tool an agent can reach, beside the real one.

WHY THIS SITS NEXT TO ``prepare_purchase``. That tool ends where every other Gecko path
ends: UNSIGNED bytes plus a receipt saying they would land. An agent that has never bought
from a storefront still has to take the last step on faith, with a funded wallet, against
mainnet. This tool is the same purchase taken all the way through — funded, signed, sent,
confirmed and then JUDGED by what the ledger shows moved — on a local surfpool fork of
mainnet. Same store, same product, same code path
(:func:`gecko.prepare_purchase.prepare_purchase_result` is what plans it), different chain.

THE ONE THING THIS FILE MUST GET RIGHT is the order:

    validate arguments  ->  is this endpoint on THIS machine?  ->  prove_surfnet()
    ->  ephemeral_signer(proof)  ->  rehearse

No key material exists above that third arrow. :func:`~gecko.sandbox.surfnet.prove_surfnet`
does a real round-trip to ``surfnet_getSurfnetInfo``, a method mainnet does not implement,
and :func:`~gecko.sandbox.surfnet.ephemeral_signer` takes the *proof* it returns rather
than a URL — so "sign against whatever endpoint the caller named" has no spelling here.
A misconfigured or hostile ``rpc_url`` is refused BEFORE a secret exists, not after, and
the refusal names how to start a real fork instead of quietly doing the purchase somewhere
else. **There is no fallback path to mainnet, because there is no code here that could
take one.**

WHY THE ENDPOINT MUST ALSO BE LOCAL. The surface this tool mounts on is served publicly
and unauthenticated, and its standing rule is that nothing on it may hold a key. This tool
is the exception, so it is narrowed until the exception is safe:
:func:`_local_fork_refusal` admits only an address on this machine (loopback, IPv4-mapped
included). A stranger therefore cannot aim it at a fork of their own, and the only key it
can ever create is bound to a validator running beside the process that created it —
unfunded on every real network, and spendable only on a chain that is thrown away. A
hosted deployment simply has no surfnet on its loopback, so the tool refuses there and
says so.

WHAT COMES BACK is the :class:`~gecko.sandbox.rehearse.Rehearsal`, projected to JSON with
``sandbox: true`` on EVERY response — success, refusal and blocked alike. The projection
carries ``what_this_does_not_prove`` for the same reason the module it wraps carries a
paragraph about it: the fork charged 42,494 CU for a purchase mainnet simulates at 36,399
(measured, same minute), so a budget sized here is sized wrong, and a rehearsal that let an
agent believe otherwise would be worse than no rehearsal.

Thin by design — parse arguments, call the package, format output. Nothing is persisted,
and no message leaving this module carries key bytes: every reason string is passed through
the sandbox's pattern-based redactor first.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal
from urllib.parse import urlsplit

from ..rpc import RpcCall, RpcError
from ..simulate import BuildCall
from ..store_accounts import (
    ProductNotOnMenu,
    StoreResolutionError,
)
from .cheatcodes import CheatcodeError
from .rehearse import Rehearsal, RehearsalError, rehearse_purchase
from .surfnet import (
    SURFNET_INFO_METHOD,
    NotASurfnetError,
    SurfnetProof,
    _redact,
    ephemeral_signer,
    prove_surfnet,
)

__all__ = [
    "HOW_TO_START_A_FORK",
    "TRY_PURCHASE_TOOL",
    "TryPurchaseRefusalCode",
    "try_purchase_result",
]

#: The command that produces the thing this tool needs, verbatim. A refusal an agent
#: cannot act on is a shrug — this is the difference between "no fork" and "here is a fork".
HOW_TO_START_A_FORK = (
    "surfpool start --no-tui --no-deploy "
    "--rpc-url https://api.mainnet-beta.solana.com --port 8899"
)

#: The refusals this tool can produce. Three names are borrowed verbatim from
#: :data:`gecko.prepare_purchase.PrepareRefusalCode` because they state the same fact about
#: the same purchase — one word means one thing on both tools — and the rest name the only
#: things that are specific to rehearsing on a fork.
TryPurchaseRefusalCode = Literal[
    "argument-invalid",
    "store-unknown",
    "product-unknown",
    "not-a-local-fork",
    "no-fork-proved",
    "fork-unusable",
    "binding-broken",
    "rehearsal-refused",
]

#: What a green rehearsal still does not tell you. Every line is a measured asymmetry
#: between this fork and the chain it forks (see :mod:`gecko.sandbox.rehearse`), carried in
#: the RESULT rather than only in the description, because a description is read once and a
#: result is read every time.
WHAT_THIS_DOES_NOT_PROVE = (
    "A FORK IS NOT A COMPUTE ORACLE. Measured: the same purchase simulated at 36,399 CU "
    "on mainnet and 42,494 here (+16.7%), and the fork then charged exactly what it "
    "simulated. Do not size a compute budget from `compute`.",
    "THE RENT IS MISSING. The buyer's token account was created by a cheatcode, so the "
    "~2,039,280 lamports a real first-time buyer pays to open one never appears in "
    "`moved.buyer_sol` — a flow that never creates one passes here and fails live.",
    "THE WINDOW IS NOT THE WINDOW. `window_blocks` is arithmetically real but a fork's "
    "block height tracks its own produced blocks, so it is not the deadline a real buyer "
    "races against.",
    "THE STATE IS THE FORK'S. Balances, prices and the store's own account are mainnet's "
    "as of the moment the fork was started, and every write this made was dropped again.",
)


def _refuse(code: TryPurchaseRefusalCode, reason: str, **extra: Any) -> dict[str, Any]:
    """A refusal, shaped like :func:`gecko.prepare_purchase._refuse` and marked sandbox.

    ``sandbox`` is set here as well as on the success path so that EVERY object this tool
    can return carries it: an agent that only ever sees a refusal must still be able to
    tell that this tool never had anything to do with mainnet.
    """
    out: dict[str, Any] = {
        "sandbox": True,
        "refused": True,
        "code": code,
        "reason": _redact(reason),
    }
    out.update(extra)
    return out


def _pin_localhost(rpc_url: str) -> str:
    """Rewrite a bare ``localhost`` host to ``127.0.0.1`` WITHOUT asking a resolver.

    `localhost` is the one name an agent will reasonably type, and it is the one name
    whose answer we already know. Substituting the literal keeps DNS out of the path
    entirely rather than trusting it to say what it is supposed to say — a hosts file, a
    search domain or a resolver cache can each make `localhost` mean something else.
    Every other name is refused by :func:`_local_fork_refusal`; this rewrites, it never
    resolves.
    """
    parts = urlsplit(rpc_url)
    if (parts.hostname or "").lower() != "localhost":
        return rpc_url
    port = f":{parts.port}" if parts.port else ""
    return parts._replace(netloc=f"127.0.0.1{port}").geturl()


def _local_fork_refusal(rpc_url: str) -> str | None:
    """``None`` if ``rpc_url`` is an endpoint on THIS machine, else why it is refused.

    The inverse of :func:`gecko.netguard.validate_public_url`, and deliberately so: that
    guard exists because an unauthenticated surface must not be turned into a proxy into
    private space, and this one exists because a tool that can sign must not be turned into
    a signer for somebody else's network.

    **A LITERAL LOOPBACK ADDRESS, AND NOTHING ELSE.** This used to resolve a hostname and
    require every answer to be loopback, which reads as thorough and is not: the transport
    then resolves the name AGAIN. An adversarial pass measured two independent
    ``getaddrinfo`` calls per invocation with nothing pinned, so a name answering
    ``127.0.0.1`` here and something else there is a rebinding away from turning this
    process into an authenticated proxy — and, if the rebound host runs a surfnet of its
    own, into a signer for somebody else's chain.

    Pinning the resolved address would work and would have to be plumbed through the
    transport. Refusing names does not: a fork on this machine is reachable at
    ``127.0.0.1``, ``localhost`` is rewritten to it below without asking a resolver, and
    there is no third case worth a DNS lookup. The class of bug is removed rather than
    narrowed, which is the difference between a guard and a race.
    """
    parts = urlsplit(rpc_url)
    if parts.scheme.lower() not in {"http", "https"}:
        return (
            f"{rpc_url!r} is not an http(s) endpoint — a rehearsal talks JSON-RPC to a "
            "surfpool fork on this machine and nothing else"
        )
    host = parts.hostname
    if not host:
        return f"{rpc_url!r} names no host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return (
            f"{host!r} is a name, not an address. A rehearsal signs, so its endpoint must "
            "be a literal loopback address — resolving a name here and letting the "
            "transport resolve it again is a rebinding window, not a check. Use "
            f"127.0.0.1 (or localhost, which is rewritten to it): {HOW_TO_START_A_FORK}"
        )
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) IS loopback; unmap so it is judged as one.
    unmapped = getattr(address, "ipv4_mapped", None) or address
    if not unmapped.is_loopback:
        return (
            f"{host!r} is not on this machine. A rehearsal signs, and it may only sign "
            "for a fork running beside this process — pointing it at a remote endpoint "
            "is exactly the move the surfnet proof exists to make impossible. Start one "
            f"locally: {HOW_TO_START_A_FORK}"
        )
    return None


def _int_argument(
    value: Any, *, name: str, low: int, high: int
) -> tuple[int, str | None]:
    """(value, refusal reason). Untrusted input — never trusted to be an int in range."""
    if value is None:
        return low, None
    if isinstance(value, bool) or not isinstance(value, int):
        return low, f"`{name}` must be a whole number between {low} and {high}"
    if not low <= value <= high:
        return low, f"`{name}` must be between {low} and {high}, got {value}"
    return value, None


def try_purchase_result(
    arguments: Any,
    *,
    rpc_call: RpcCall | None = None,
    build_call: BuildCall | None = None,
) -> dict[str, Any]:
    """Rehearse one purchase on a proven local fork, and answer with what moved.

    ``rpc_call``/``build_call`` are the injected transports, keyword-only for the same
    reason they are on :func:`gecko.prepare_purchase.prepare_purchase_result`: they are
    chosen by whoever mounts the function and are unreachable from ``arguments``, so no
    caller can substitute a transport.

    Returns a dict — never raises for a caller's mistake, an unreachable fork, or a
    storefront that does not exist. Those are answers.
    """
    args = arguments if isinstance(arguments, dict) else {}

    store = args.get("store")
    product = args.get("product")
    if not isinstance(store, str) or not store.strip():
        return _refuse(
            "argument-invalid",
            "name the store to rehearse against — `list_stores` shows what exists. "
            "Nothing here substitutes a default store for one you did not name.",
        )
    if not isinstance(product, str) or not product.strip():
        return _refuse(
            "argument-invalid", "name the `product`, exactly as the store lists it"
        )
    if args.get("buyer") is not None:
        # Refused rather than ignored. Accepting a buyer and then spending from a
        # different account would make the one field an agent copies across from
        # `prepare_purchase` a lie about whose money moved.
        return _refuse(
            "argument-invalid",
            "`try_purchase` takes no `buyer`. It pays with a keypair generated inside "
            "this call, AFTER the endpoint proved it is a local fork, funded by "
            "cheatcode and discarded when the run ends — so the rehearsal needs no "
            "wallet, no signer and no funds. Use `prepare_purchase` when you want a "
            "named buyer to sign for real.",
        )
    table, table_refusal = _int_argument(
        args.get("table"), name="table", low=0, high=255
    )
    if table_refusal:
        return _refuse("argument-invalid", table_refusal)

    raw_url = args.get("rpc_url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return _refuse(
            "argument-invalid",
            "`rpc_url` is required and there is no default: a rehearsal runs on YOUR "
            "fork, and defaulting one would mean guessing which chain to spend on. "
            f"Start a fork — {HOW_TO_START_A_FORK} — and pass "
            "`rpc_url=http://127.0.0.1:8899`.",
        )
    rpc_url = _pin_localhost(raw_url.strip())

    local_refusal = _local_fork_refusal(rpc_url)
    if local_refusal:
        return _refuse("not-a-local-fork", local_refusal, rpc_url=rpc_url)

    # THE GATE. No key exists on either side of this call; only its success creates the
    # possibility of one. Everything above is argument handling, and everything below
    # depends on a `SurfnetProof` that cannot be constructed without a validator having
    # answered a method mainnet does not implement.
    try:
        proof = prove_surfnet(rpc_url, rpc_call)
    except (NotASurfnetError, RpcError) as exc:
        # THE CLASS, NEVER THE TEXT. This branch used to interpolate `exc`, which meant
        # whatever the endpoint said came back to the caller verbatim. An adversarial pass
        # pointed it at a service that answers with a banner and read
        # `INTERNAL-SERVICE-BANNER-s3cr3t-token-abc123` straight out of the refusal. On a
        # hosted deployment loopback is OUR machine, so a stranger echoing arbitrary local
        # error text is a read primitive, not a diagnostic. The class is all a caller needs
        # to know what to do next; the text is for our logs.
        return _refuse(
            "no-fork-proved",
            f"nothing at {rpc_url} proved it is a local surfnet — it is unreachable, or "
            f"it is a real network ({type(exc).__name__}). NOTHING WAS SIGNED, SENT OR "
            "EVEN PREPARED, and this tool has no path that falls back to mainnet: start "
            f"a fork and try again — {HOW_TO_START_A_FORK}",
            rpc_url=rpc_url,
            proof_method=SURFNET_INFO_METHOD,
        )

    buyer = ephemeral_signer(proof)
    try:
        rehearsal = rehearse_purchase(
            proof,
            buyer=buyer,
            store=store.strip(),
            product=product.strip(),
            table_number=table,
            rpc_call=rpc_call,
            build_call=build_call,
        )
    except ProductNotOnMenu as exc:
        return _refuse("product-unknown", str(exc), rpc_url=rpc_url)
    except StoreResolutionError as exc:
        return _refuse("store-unknown", str(exc), rpc_url=rpc_url)
    except RehearsalError as exc:
        # A binding did not hold, which means the run stopped BEFORE it could sign or send.
        return _refuse("binding-broken", str(exc), rpc_url=rpc_url)
    except (CheatcodeError, RpcError) as exc:
        return _refuse(
            "fork-unusable",
            f"the fork at {rpc_url} answered, but the rehearsal could not be carried out "
            f"on it: {exc}",
            rpc_url=rpc_url,
        )
    return _to_json(rehearsal, proof=proof)


def _delta(delta: Any) -> dict[str, Any] | None:
    """A :class:`~gecko.sandbox.rehearse.TokenDelta`/``LamportDelta`` as JSON.

    ``moved`` is carried explicitly rather than left for the reader to subtract: ``None``
    on either side means "the account did not exist", which is not zero, and an agent that
    computed the difference itself would turn that distinction into one.
    """
    if delta is None:
        return None
    out: dict[str, Any] = {
        "before": delta.before,
        "after": delta.after,
        "moved": delta.moved,
    }
    account = getattr(delta, "account", None)
    if account is not None:
        out["account"] = account
        out["owner"] = delta.owner
        out["mint"] = delta.mint
    else:
        out["address"] = delta.address
    return out


def _to_json(rehearsal: Rehearsal, *, proof: SurfnetProof) -> dict[str, Any]:
    """The rehearsal as an agent reads it. Every number came off the fork's own chain.

    ``refused`` is TRUE when a step declined — the run funded, drove the production path,
    and that path said no. It is the same word the other tools on this surface use for the
    same event, so an agent branching on it does not have to know it called a rehearsal;
    the whole projection still rides along underneath, because a refusal from inside the
    loop is more informative than a bare code, not less. A run that LANDED and moved the
    wrong money is NOT refused: read ``discrepancies`` for that, which is why it is not
    collapsed into this flag.
    """
    simulated = rehearsal.simulated_units
    charged = rehearsal.units_consumed
    declined = rehearsal.refusals[0] if rehearsal.refusals else None
    out: dict[str, Any] = {
        "sandbox": True,
        "refused": declined is not None,
        "network": "fork",
        "rpc_url": proof.rpc_url,
        "proved": {"method": SURFNET_INFO_METHOD, "slot": proof.slot},
        "store": rehearsal.store,
        "product": rehearsal.product,
        "price_raw": rehearsal.price_raw,
        "mint": rehearsal.mint,
        # The public key of the throwaway that paid. It exists only inside this call; the
        # secret half was never written, logged, returned or serialised.
        "buyer": rehearsal.buyer,
        "landed": rehearsal.landed,
        "signature": rehearsal.signature,
        "balanced": rehearsal.balanced,
        "moved": {
            "buyer_token": _delta(rehearsal.buyer_token),
            "store_token": _delta(rehearsal.store_token),
            "buyer_sol": _delta(rehearsal.buyer_sol),
        },
        "receipt": (
            None
            if rehearsal.receipt is None
            else {
                "receipt_id": rehearsal.receipt.receipt_id,
                "product_name": rehearsal.receipt.product_name,
                "price_raw": rehearsal.receipt.price_raw,
                "table_number": rehearsal.receipt.table_number,
                "delivered": rehearsal.receipt.delivered,
                "total_purchases_before": rehearsal.receipt.total_purchases_before,
                "total_purchases_after": rehearsal.receipt.total_purchases_after,
            }
        ),
        "compute": {
            "simulated_units": simulated,
            "units_consumed": charged,
            # Stated, not implied: on the fork these agreed to the unit, and that
            # agreement is a fact about the fork, not about mainnet.
            "agree": None
            if simulated is None or charged is None
            else simulated == charged,
        },
        "fee_lamports": rehearsal.fee_lamports,
        "window_blocks": rehearsal.window_blocks,
        # The ledger's own objections. An empty list is the only passing value, and it is
        # listed above `refusals` because a run CAN land, confirm and still be wrong.
        "discrepancies": [_redact(problem) for problem in rehearsal.discrepancies],
        "refusals": [
            {"step": refusal.step, "reason": _redact(refusal.reason)}
            for refusal in rehearsal.refusals
        ],
        # Cleanup diagnostics, and the redactor costs something visible here: a base58
        # ADDRESS is 44 characters, so each line reads `<redacted>: N -> M lamports`. That
        # is the deliberate trade — the pattern cannot tell a pubkey from a seed, and the
        # addresses are already carried untouched in `moved` and `buyer`, so nothing is
        # lost that this response does not state elsewhere.
        "reset": [_redact(line) for line in rehearsal.reset],
        "what_this_does_not_prove": list(WHAT_THIS_DOES_NOT_PROVE),
    }
    if declined is not None:
        out["code"] = "rehearsal-refused"
        out["reason"] = _redact(f"[{declined.step}] {declined.reason}")
    return out


TRY_PURCHASE_TOOL: dict[str, Any] = {
    "name": "try_purchase",
    "description": (
        "REHEARSE a real purchase on a local fork of mainnet, and see what actually "
        "moved. This is `prepare_purchase` taken all the way through: it funds a "
        "throwaway buyer with cheatcodes, plans the purchase with the SAME code the real "
        "tool runs, signs it, sends it, waits for the fork to confirm, and then reads the "
        "chain back — the buyer's token account, the store's, the SOL, and the receipt "
        "row the program itself wrote. "
        "IT LANDS FOR REAL ON THAT FORK: a signature is produced and a transaction is "
        "executed. It just happens on a chain that is thrown away, with a key that is "
        "created inside the call and discarded when it returns. "
        "IT CAN NEVER TOUCH MAINNET. The key cannot exist until the endpoint has answered "
        "`surfnet_getSurfnetInfo`, a method only a surfpool fork implements, and the "
        "endpoint must be on the machine running this server. A URL that is neither is "
        "REFUSED before any key exists — there is no fallback that quietly buys somewhere "
        "else. "
        "USE IT to see the whole loop before spending, to check a store really debits "
        "what it lists, or to show a buyer what will happen. It needs NO wallet, NO signer "
        "and NO funds. "
        "A FORK IS NOT A COMPUTE ORACLE: measured, the same purchase simulates at 36,399 "
        "CU on mainnet and 42,494 on the fork (+16.7%), so never size a compute budget "
        "from what comes back. Nor is it a price oracle for the fee: the buyer's token "
        "account is conjured by a cheatcode, so the ~0.002 SOL of rent a first-time buyer "
        "really pays is missing. Every response carries `sandbox: true` and a "
        "`what_this_does_not_prove` list — read it before quoting a number from here. "
        "WHEN NOT TO USE IT: this buys nothing for the human. When they actually want the "
        "product, call `prepare_purchase` and have their wallet sign it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "store": {
                "type": "string",
                "description": (
                    "the storefront's name, exactly as it appears on chain — the same "
                    "name you would pass to `prepare_purchase`. The fork mirrors mainnet, "
                    "so the store, its products and its prices are the real ones"
                ),
            },
            "product": {"type": "string", "description": "the product's name"},
            "table": {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "description": "the table number the order is for (u8)",
            },
            "rpc_url": {
                "type": "string",
                "description": (
                    "your local surfpool fork, e.g. `http://127.0.0.1:8899`. REQUIRED and "
                    "never defaulted, and it must be an address on this machine that "
                    "answers `surfnet_getSurfnetInfo`. Start one with: "
                    + HOW_TO_START_A_FORK
                ),
            },
        },
        "required": ["store", "product", "rpc_url"],
        "additionalProperties": False,
    },
}
