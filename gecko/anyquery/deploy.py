"""The hosted any-query deps, built from settings and the keychain.

:func:`gecko.anyquery.hosted.anyquery_deps_from_env` calls :func:`deps_from_settings`
and turns :class:`NotServedError` into "no mount, reason logged". The settings and slots
are listed in :mod:`gecko.anyquery.hosted`. Every error names a setting or a keychain
slot, never a value; the RPC URL may carry a provider key and is never echoed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..credentials import ChainResolver, CredentialError, CredentialRef
from ..servicemap.snapshot import CatalogSourceError, catalog_source
from ..networks import Network
from .config import ConfigError, load_config
from .service import AnyQueryDeps

__all__ = [
    "CAP_ENV",
    "DATA_ENV",
    "DETAILS_ENV",
    "LEDGERS_ENV",
    "NETWORK_ENV",
    "NotServedError",
    "PublicFetchRefusedError",
    "RPC_ENV",
    "deps_from_settings",
]

DETAILS_ENV = "GECKO_ANYQUERY_DETAILS_DIR"
LEDGERS_ENV = "GECKO_ANYQUERY_LEDGERS_DIR"
DATA_ENV = "GECKO_ANYQUERY_DATA_DIR"
CAP_ENV = "GECKO_ANYQUERY_UPSTREAM_CAP_CENTS"
RPC_ENV = "GECKO_ANYQUERY_RPC_URL"
NETWORK_ENV = "GECKO_ANYQUERY_NETWORK"
#: Where the registry's wallet bindings live; ``enrol.MongoWalletBinder`` writes there.
_WALLETS_DB, _WALLETS_COLLECTION = "gecko_registry", "wallets"

IdlFetch = Callable[[str], dict[str, Any]]


class NotServedError(Exception):
    """Why the mount is absent. Names a setting or a keychain slot, never a value."""


class PublicFetchRefusedError(Exception):
    """The hosted runner was asked to fetch a public URL. It never does."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise NotServedError(f"{name} is not set")
    return value


def _cap_cents(env: Mapping[str, str]) -> int:
    raw = _required(env, CAP_ENV)
    if not raw.isdigit() or int(raw) <= 0:
        raise NotServedError(f"{CAP_ENV} must be a positive whole number of cents")
    return int(raw)


def _rpc_url(env: Mapping[str, str]) -> str:
    url = _required(env, RPC_ENV)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise NotServedError(f"{RPC_ENV} must be an https URL")
    return url


def _network(env: Mapping[str, str]) -> Network:
    network = _required(env, NETWORK_ENV)
    if network == "mainnet":
        return "mainnet"
    if network == "devnet":
        return "devnet"
    raise NotServedError(f"{NETWORK_ENV} must be 'mainnet' or 'devnet'")


def _require_slots(resolver: ChainResolver, refs: Iterable[CredentialRef]) -> None:
    for ref in refs:
        try:
            resolver.resolve(ref)  # presence only: the value is dropped here
        except CredentialError:
            raise NotServedError(
                f"no {ref.slot()!r} in the keychain; set it with: gecko auth set "
                f"{ref.slot()}"
            ) from None


def _orquestra_idl_fetch() -> IdlFetch:
    """The Whirlpool IDL for a SOL payment, fetched on first use, not at mount time."""
    held: list[IdlFetch] = []

    def fetch(program_id: str) -> dict[str, Any]:
        if not held:
            from ..orquestra_build import orquestra_seams

            held.append(orquestra_seams()[0])
        return held[0](program_id)

    return fetch


def _refuse_public_fetch(_url: str) -> tuple[int, str]:
    # The runner skips public-catalog sources today; reaching this is a bug, and a
    # hosted process must not fetch an arbitrary URL on its behalf.
    raise PublicFetchRefusedError("the hosted any-query runner makes no public fetch")


def deps_from_settings(
    env: Mapping[str, str],
    resolver: ChainResolver,
    *,
    connect: Callable[[str], Any] | None = None,
    swap_idl_fetch: IdlFetch | None = None,
) -> AnyQueryDeps:
    """Every lane from ``env`` and ``resolver``, or :class:`NotServedError` naming the
    first setting or keychain slot that is missing or refused."""
    from ..orthogonal.api import OrthogonalApi, live_transport
    from ..orthogonal.job import JobDeps
    from ..orthogonal.session import ORTHOGONAL_REF, orthogonal_session
    from ..privy_backend import CONFIG_NAMES
    from ..registry.wallets import MongoWalletDirectory
    from ..rpc import default_rpc_call
    from .billing import BillingError
    from ..servicemap.mongo import MongoRegistry
    from ..servicemap.pins import pay_to_resolver
    from ..servicemap.registry import RegistryError
    from ..servicemap.snapshot import load_from_registry
    from .catalog_load import load_catalog
    from .execute import execute
    from .model_planner import (
        ANTHROPIC_REF,
        DEFAULT_PLANNER_MODEL,
        ModelPlanner,
        anthropic_call,
    )
    from .mongo import connect_mongo, mongo_store_from_keychain
    from .payment_tx import whirlpool_sol_pricer
    from .rebates import MongoRebateIndex
    from .retrieve import bm25_candidates
    from .run import RunDeps
    from .settlement import Settlement, settlement_backend_for
    from .settlement_tx import whirlpool_swap_leg
    from .solana_billing import SolanaBilling, privy_backend_for
    from .store import StoreError
    from .upstream_spend import MongoSpendLedger
    from .upstream_x402 import OrthogonalX402
    from .x402_policy import x402_pay_policy_from_config
    from .hosted import payee_usdc_ata
    from .validate import validator_for

    try:
        config = load_config(env)
    except ConfigError as refused:
        raise NotServedError(str(refused)) from None
    if config.billing != "solana":
        raise NotServedError(
            "GECKO_ANYQUERY_BILLING is not 'solana': every run is charged before it "
            "calls a source, and fake billing is never served"
        )
    try:
        source = catalog_source(env)
    except CatalogSourceError as refused:
        raise NotServedError(str(refused)) from None
    details = Path(_required(env, DETAILS_ENV)) if source == "files" else None
    if details is not None and not details.is_dir():
        raise NotServedError(f"{DETAILS_ENV} is not a folder")
    cap = _cap_cents(env)
    rpc_url = _rpc_url(env)
    network = _network(env)
    data = Path(env.get(DATA_ENV) or Path.home() / ".gecko" / "anyquery")
    # PRIVY_WALLET_ID is per user: billing passes the bound wallet, never the slot.
    privy = [CredentialRef(n) for n in CONFIG_NAMES if n != "PRIVY_WALLET_ID"]
    # x402 pays Orthogonal from the settlement wallet, so the key is not needed then.
    pay_x402 = config.orthogonal_pay == "x402"
    upstream = [] if pay_x402 else [ORTHOGONAL_REF]
    _require_slots(resolver, [ANTHROPIC_REF, *upstream, *privy])

    ledgers_dir = (env.get(LEDGERS_ENV) or "").strip()
    ledgers = sorted(Path(ledgers_dir).glob("*.jsonl")) if ledgers_dir else []
    if details is not None:
        loaded = load_catalog(details, ledgers=ledgers)
        if not loaded.candidates.candidates:
            raise NotServedError(f"no catalog candidates in {DETAILS_ENV}")

    try:
        store = mongo_store_from_keychain(
            resolver=resolver, connect=connect or connect_mongo
        )
    except StoreError as refused:  # names the slot, never the connection string
        raise NotServedError(str(refused)) from None
    if details is None:
        # GECKO_ANYQUERY_CATALOG=registry: the planner's pool comes from gecko_registry.
        try:
            loaded = load_from_registry(
                MongoRegistry(store.client, unavailable=store.unavailable),
                now=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
        except RegistryError as refused:
            raise NotServedError(str(refused)) from None
        if not loaded.candidates.candidates:
            raise NotServedError("no catalog candidates in the service registry")
    try:
        idl_fetch = swap_idl_fetch or _orquestra_idl_fetch()
        pool = config.settlement_pool
        # SOL is accepted only with the settlement wallet AND its pool configured: the
        # pool prices the payment and the wallet settles it (option B, 9 Oct).
        sol_ready = bool(pool and config.settlement_wallet_id)
        settlement = (
            Settlement(
                config=config,
                store=store,
                rpc_url=rpc_url,
                network=network,
                backend=settlement_backend_for(config, network, resolver=resolver),
                swap_planner=whirlpool_swap_leg(
                    rpc_url=rpc_url,
                    rpc_call=default_rpc_call,
                    idl_fetch=idl_fetch,
                    slippage_bps=config.swap_slippage_bps,
                    pool=pool or "",
                ),
                resolver=resolver,
            )
            if sol_ready
            else None
        )
        billing = SolanaBilling(
            config=config,
            store=store,
            wallets=MongoWalletDirectory(
                collection=store.client[_WALLETS_DB][_WALLETS_COLLECTION]
            ),
            rpc_url=rpc_url,
            network=network,
            backend_for=privy_backend_for(network, resolver=resolver),
            sol_pricer=whirlpool_sol_pricer(
                rpc_url=rpc_url,
                rpc_call=default_rpc_call,
                idl_fetch=idl_fetch,
                pool=pool or "",
                margin_bps=config.sol_payment_margin_bps,
            )
            if sol_ready
            else None,
            settlement=settlement,
            rebate_index=MongoRebateIndex(store.client),
        )
    except BillingError as refused:
        raise NotServedError(str(refused)) from None

    model = config.planner_model or DEFAULT_PLANNER_MODEL
    x402 = None
    if pay_x402:
        try:
            # Each operation's payTo is pinned from gecko_registry's last challenge
            # probe; GECKO_ORTHOGONAL_X402_PAYTO, when set, overrides it for all calls.
            x402 = OrthogonalX402(
                policy=x402_pay_policy_from_config(config),
                network=network,
                rpc_url=rpc_url,
                backend=settlement_backend_for(config, network, resolver=resolver),
                pay_to_for=pay_to_resolver(
                    MongoRegistry(store.client, unavailable=store.unavailable)
                ),
            )
        except (ConfigError, BillingError) as refused:
            raise NotServedError(str(refused)) from None
    base = JobDeps(
        api=OrthogonalApi(
            transport=live_transport,
            session=orthogonal_session(resolver=resolver),
            x402=x402,
        ),
        # Mongo, not a file in the task: a deploy must not reset the host-wide cap.
        ledger=MongoSpendLedger(store.client, unavailable=store.unavailable),
        folder=data,
        mode="live",
        total_cap=cap,
    )
    return AnyQueryDeps(
        config=config,
        store=store,
        billing=billing,
        planner=ModelPlanner(
            call=anthropic_call(model, resolver=resolver), model=model
        ),
        candidates_for=bm25_candidates,
        validate_plan=validator_for(loaded.confirmed),
        catalog=lambda: loaded.candidates,
        execute=execute,
        run_deps=RunDeps(
            base=base, mode="live", store=store, public_fetch=_refuse_public_fetch
        ),
        payee_usdc_ata=lambda: payee_usdc_ata(config),
    )
