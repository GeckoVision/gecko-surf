"""Where Gecko sends a signed transaction: public RPC, or Helius with a backrun rebate.

**What the rebate is.** Helius accepts ``rebate-address=<pubkey>`` on a mainnet
``sendTransaction``. It submits the transaction and, in parallel, a bundle of [our tx,
a searcher's backrun]; if the bundle lands, a share of the backrun's value is paid in
SOL to that address. It is intermittent, and nobody has yet seen Helius pay one when a
relay is the fee payer: that is unknown until the first live swap.

**Ruling (founder, 2026-10-08): the rebate goes to the relay.** ``rebate-address`` = the
Kora relay's fee-payer address, from config (``relay_fee_payer``), so backrun rebates
refill the wallet that pays users' network fees. The guard changed from "never the fee
payer" to "only the configured relay address": this module never derives a rebate
address from the message, never takes one from a tool argument, and ignores any
``authority`` a caller names. No relay configured means no ``rebate-address`` at all.
The user's share is a discount on their next run, recorded in the ledger
(:mod:`gecko.anyquery.rebates`); the relay's hot balance has a ceiling above which the
founder sweeps to cold by hand.

**Ruling (staff, 2026-10-08): a transport, not a fifth seam.** :class:`SendTransport` is
an ``RpcCall`` value bound to one endpoint. It adds no adapter contract to the engine.

**Where it applies, and only there.** Gecko must be the party calling
``sendTransaction`` AND the message must invoke an allowlisted swap program
(:data:`REBATE_SWAP_PROGRAMS`, Whirlpool today). Transfers, store purchases and x402
payments move no pool price, so no backrun exists; they go to the same endpoint without
the parameter. The guard never refuses a send: a missing rebate must never block a
payment.

**The key.** ``HELIUS_API_KEY`` is read from the OS keychain only
(``ChainResolver([KeyringBackend()])`` — no env fallback, per the founder's "never .env"
rule), per call. The keyed URL exists as a local inside one method and is dropped before
anything can raise. Every failure is re-raised as :class:`gecko.rpc.RpcError` naming
only the label, the method and an HTTP status, OUTSIDE the ``except`` block, so it
carries neither ``__cause__`` nor ``__context__`` (``HTTPError.url`` holds the full
URL). :func:`gecko.rpc.redact_url_secrets` runs over every message as a second line.

**Off by default.** :func:`configured_send` returns the public transport unless the
operator sets ``GECKO_SEND_REBATE=1``; an unusable Helius setup (no key, not mainnet)
falls back to public ONCE, before the first send, with the reason — so every
rebroadcast of a transaction goes to the endpoint its first send used. Mixing endpoints
loses the bundle.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .credentials import ChainResolver, CredentialError, CredentialRef, KeyringBackend
from .networks import Network
from .rpc import RpcCall, RpcError, default_rpc_call, post_json, redact_url_secrets
from .whirlpool_venue import WHIRLPOOL_PROGRAM

__all__ = [
    "HELIUS_HOST",
    "HELIUS_KEY_REF",
    "HELIUS_LABEL",
    "REBATE_FLAG_ENV",
    "REBATE_SWAP_PROGRAMS",
    "HeliusSend",
    "PublicSend",
    "RebateDecision",
    "SendConfigError",
    "SendTransport",
    "configured_send",
    "helius_send",
    "rebate_enabled",
    "relay_rebate_for",
]

logger = logging.getLogger(__name__)

HELIUS_HOST = "mainnet.helius-rpc.com"
#: The ONLY printable name of the keyed endpoint. Receipts, errors and logs use this.
HELIUS_LABEL = "helius-mainnet"
HELIUS_KEY_REF = CredentialRef(api="HELIUS_API_KEY")
#: The operator switch. Exactly ``"1"`` turns it on; anything else is off.
REBATE_FLAG_ENV = "GECKO_SEND_REBATE"
#: Programs whose instructions move a pool price, so a backrun can exist. A program is
#: added here only once a swap through it is one Gecko itself submits.
REBATE_SWAP_PROGRAMS: frozenset[str] = frozenset({WHIRLPOOL_PROGRAM})

PostJson = Callable[[str, bytes], dict[str, Any]]


class SendConfigError(Exception):
    """The Helius transport cannot be built. Names the cause, never a key or a URL."""


@dataclass(frozen=True)
class RebateDecision:
    """What the transport will do about a rebate for one transaction. Agent-safe."""

    requested: bool
    endpoint: str
    address: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "endpoint": self.endpoint,
            "address": self.address,
            "reason": self.reason,
        }


@runtime_checkable
class SendTransport(Protocol):
    """An ``RpcCall`` bound to one endpoint: ``(method, params) -> response``."""

    @property
    def label(self) -> str:
        """The endpoint's printable name; never a URL."""
        ...

    @property
    def mainnet_only(self) -> bool:
        """True for a mainnet relay: a caller on any other network must not hand it a
        transaction it then polls for somewhere else."""
        ...

    def rebate_for(
        self, transaction_base64: str, *, authority: str | None = None
    ) -> RebateDecision: ...

    def __call__(
        self, method: str, params: list[Any], *, authority: str | None = None
    ) -> dict[str, Any]: ...


def _is_pubkey(text: str) -> bool:
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(text)
    except Exception:  # noqa: BLE001 - any parse failure means "not an address"
        return False
    return True


def relay_rebate_for(
    transaction_base64: str, *, relay_fee_payer: str | None
) -> tuple[str | None, str]:
    """The configured relay for a swap through an allowlisted program, else ``None`` and
    why. The ONLY way a rebate address is chosen.

    Pure: decodes the message and reads only its static account keys and instruction
    program ids. Program ids are always static, even in a v0 message with lookup tables,
    so the swap test is complete without resolving any table. The relay does not have
    to sign: it is the operator's address, set in config.
    """
    import base64

    from .txbind import TxDecodeError, _message_of

    if relay_fee_payer is None:
        return None, "no relay fee-payer address is configured; no rebate is requested"
    try:
        message, _version = _message_of(base64.b64decode(transaction_base64))
        keys = [str(key) for key in message.account_keys]
        programs = {keys[ix.program_id_index] for ix in message.instructions}
    except (TxDecodeError, ValueError, IndexError, TypeError):
        return None, "the transaction did not decode, so it is sent without a rebate"
    if not programs & REBATE_SWAP_PROGRAMS:
        return None, "not a swap through an allowlisted program; no backrun exists"
    return relay_fee_payer, "the configured relay fee payer"


def rebate_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(REBATE_FLAG_ENV) == "1"


@dataclass(frozen=True)
class PublicSend:
    """Today's behaviour: the same ``RpcCall`` to the same URL, no rebate."""

    #: Kept out of repr: a provider URL can carry ``?api-key=``.
    rpc_url: str = field(repr=False)
    rpc_call: RpcCall = default_rpc_call
    #: Why no rebate is requested. ``None`` = the operator never turned it on.
    note: str | None = None
    label: str = field(default="public-rpc")
    mainnet_only: bool = False

    def rebate_for(
        self, transaction_base64: str, *, authority: str | None = None
    ) -> RebateDecision:
        return RebateDecision(
            requested=False,
            endpoint=self.label,
            address=None,
            reason=self.note or "rebate routing is off on this server",
        )

    def __call__(
        self, method: str, params: list[Any], *, authority: str | None = None
    ) -> dict[str, Any]:
        return self.rpc_call(self.rpc_url, method, params)


class HeliusSend:
    """Helius mainnet. Holds a resolver and a post function, never a key or a URL."""

    __slots__ = ("_resolver", "_post", "_mev_protect", "_relay")
    label = HELIUS_LABEL
    mainnet_only = True

    def __init__(
        self,
        *,
        resolver: ChainResolver,
        post: PostJson,
        mev_protect: bool = False,
        relay_fee_payer: str | None = None,
    ) -> None:
        if relay_fee_payer is not None and not _is_pubkey(relay_fee_payer):
            raise SendConfigError("the configured relay fee payer is not an address")
        self._resolver = resolver
        self._post = post
        self._mev_protect = mev_protect
        self._relay = relay_fee_payer

    def _address(self, transaction_base64: str) -> tuple[str | None, str]:
        # ``authority`` is accepted by the Protocol and deliberately never read here:
        # the only rebate address is the configured relay (founder ruling, 8 Oct).
        return relay_rebate_for(transaction_base64, relay_fee_payer=self._relay)

    def __repr__(self) -> str:
        return f"<SendTransport {self.label}>"

    __str__ = __repr__

    def rebate_for(
        self, transaction_base64: str, *, authority: str | None = None
    ) -> RebateDecision:
        address, reason = self._address(transaction_base64)
        return RebateDecision(
            requested=address is not None,
            endpoint=self.label,
            address=address,
            reason=reason,
        )

    def __call__(
        self, method: str, params: list[Any], *, authority: str | None = None
    ) -> dict[str, Any]:
        query: dict[str, str] = {}
        if method == "sendTransaction":
            if params and isinstance(params[0], str):
                address, _ = self._address(params[0])
                if address is not None:
                    query["rebate-address"] = address
            if self._mev_protect:
                query["mev-protect"] = "true"
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()

        response, failure = self._post_keyed(method, query, body)
        if failure is not None:
            # Raised here, outside every `except`, so the exception has no __context__
            # holding the HTTPError (whose `.url` is the keyed URL).
            raise RpcError(redact_url_secrets(failure))
        assert response is not None
        error = response.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
            raise RpcError(
                redact_url_secrets(
                    f"{self.label}: JSON-RPC {method} failed: code={code} "
                    f"message={message!r}"
                )
            )
        return response

    def _post_keyed(
        self, method: str, query: dict[str, str], body: bytes
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The one place the key exists. Returns ``(response, None)`` or
        ``(None, safe_message)``; never raises, so no frame up the stack sees a URL."""
        try:
            key = self._resolver.resolve(HELIUS_KEY_REF)
        except CredentialError:
            return None, f"{self.label}: no HELIUS_API_KEY in keychain"
        url = f"https://{HELIUS_HOST}/?" + urllib.parse.urlencode(
            {"api-key": key, **query}
        )
        failure: str | None = None
        response: dict[str, Any] | None = None
        try:
            response = self._post(url, body)
        except urllib.error.HTTPError as exc:
            failure = f"{self.label}: {method} failed: HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - re-raised redacted by the caller
            failure = f"{self.label}: {method} failed: {type(exc).__name__}"
        finally:
            del key, url
        return response, failure


def _keychain_only() -> ChainResolver:
    return ChainResolver([KeyringBackend()])


def helius_send(
    *,
    network: Network,
    resolver: ChainResolver | None = None,
    post: PostJson = post_json,
    mev_protect: bool = False,
    relay_fee_payer: str | None = None,
) -> HeliusSend:
    """Build the Helius transport, or raise :class:`SendConfigError` before any POST.

    Checked once, here, so a caller decides its endpoint BEFORE the first send and
    keeps it for every rebroadcast. ``unknown`` is refused: a network is asserted,
    never inferred, and Helius skips devnet anyway.
    """
    if network != "mainnet":
        raise SendConfigError(
            f"the Helius rebate transport is mainnet only; this send is on {network!r}"
        )
    chain = resolver if resolver is not None else _keychain_only()
    try:
        chain.resolve(HELIUS_KEY_REF)
    except CredentialError:
        raise SendConfigError(
            "no HELIUS_API_KEY in keychain (set it with: gecko auth set HELIUS_API_KEY)"
        ) from None
    return HeliusSend(
        resolver=chain,
        post=post,
        mev_protect=mev_protect,
        relay_fee_payer=relay_fee_payer,
    )


def configured_send(
    rpc_url: str,
    *,
    network: Network,
    rpc_call: RpcCall | None = None,
    environ: Mapping[str, str] | None = None,
    resolver: ChainResolver | None = None,
    post: PostJson = post_json,
    relay_fee_payer: str | None = None,
) -> tuple[SendTransport, str | None]:
    """The operator's choice of send endpoint, decided once per transaction.

    Returns ``(transport, note)``. Off (the default) -> public, ``note=None``. On and
    usable -> Helius. On and unusable -> public with the reason in ``note``: never a
    refusal, because a missing rebate must not block a payment.
    """
    call = rpc_call or default_rpc_call
    if not rebate_enabled(environ):
        return PublicSend(rpc_url, call), None
    try:
        return (
            helius_send(
                network=network,
                resolver=resolver,
                post=post,
                relay_fee_payer=relay_fee_payer,
            ),
            None,
        )
    except SendConfigError as exc:
        note = f"rebate routing is on but unavailable: {exc}"
        logger.warning("%s; sending through the public endpoint", note)
        return PublicSend(rpc_url, call, note=note), note
