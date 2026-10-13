"""Any-query configuration, read once from the environment.

The fee wallet is a plain address the founder controls. Nothing in Gecko holds its key,
and it is only ever read from here: never from tool arguments, never from a message.
The split and margin VALUES belong to business-manager; this module only carries them.

The relay fee payer (founder ruling, 8 Oct) is the Kora relay's fee-payer address: the
ONLY rebate address Gecko ever asks Helius for, so backrun rebates refill the wallet
that pays users' network fees. It is funded by hand like the founder's other wallets;
the two balance thresholds only make Gecko log a reminder, they never move money.

The settlement wallet (option B, founder ruling 9 Oct) is a Privy server wallet Gecko
owns: a user who pays in SOL sends a plain transfer to it, and Gecko then swaps that SOL
to USDC on ONE Whirlpool (``GECKO_SETTLEMENT_POOL``) and forwards the USDC to the cold
treasury. Its id and address are public identifiers, not secrets. The SOL a user pays is
the quote converted at the pool's price plus ``sol_payment_margin_bps``; Gecko carries
the swap's price risk from there.

The settlement wallet keeps a USDC FLOAT (``settlement_float_usdc_micro``, default 1 USDC):
a settlement forwards to the treasury only what the wallet holds above it. The float is
what pays Orthogonal per call over x402 (``GECKO_ORTHOGONAL_PAY=x402``, founder ruling
9 Oct): the payment goes to the USDC account of the payTo pinned for that operation in
the service registry (or of ``orthogonal_x402_pay_to`` when that override is set), at
most ``max_x402_call_usdc_micro`` per call. Without that switch the API-key path is unchanged.

Dataset snapshots go to S3 when ``GECKO_DATASETS_BUCKET`` and ``GECKO_DATASETS_KMS_KEY``
are both set: the bucket name and the KMS key id (an ARN, ``alias/...`` or a key UUID).
Both are identifiers, not secrets; the task role's IAM grants the access. Neither set
keeps snapshots off S3 (``local://`` or ``pending``); one without the other is refused.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal
from ..credentials import PLATFORM_UNSET_SENTINEL

__all__ = [
    "ORTHOGONAL_PAY_ENV",
    "OrthogonalPay",
    "RELAY_FEE_PAYER_ENV",
    "SETTLEMENT_POOL_ENV",
    "SETTLEMENT_WALLET_ENV",
    "SETTLEMENT_WALLET_ID_ENV",
    "AnyQueryConfig",
    "ConfigError",
    "DATASETS_BUCKET_ENV",
    "DATASETS_KMS_KEY_ENV",
    "is_pubkey",
    "load_config",
]

#: The env name of the relay's fee-payer address (public, not a secret).
RELAY_FEE_PAYER_ENV = "GECKO_RELAY_FEE_PAYER"
#: The settlement wallet's Privy id and address, and the one pool it swaps on. Public.
SETTLEMENT_WALLET_ID_ENV = "GECKO_SETTLEMENT_WALLET_ID"
SETTLEMENT_WALLET_ENV = "GECKO_SETTLEMENT_WALLET"
SETTLEMENT_POOL_ENV = "GECKO_SETTLEMENT_POOL"
#: Privy ids are lower-case alphanumerics (``testwallet00000000000001``). Checked so an id
#: can never carry a path segment into a Privy URL.
_PRIVY_ID = re.compile(r"^[a-z0-9]{8,64}$")

#: ``key`` (the bearer key, card credits) or ``x402`` (paid per call from the settlement
#: wallet). Public, not a secret.
ORTHOGONAL_PAY_ENV = "GECKO_ORTHOGONAL_PAY"
OrthogonalPay = Literal["key", "x402"]

#: The S3 bucket and KMS key of dataset snapshots. Public identifiers, not secrets.
DATASETS_BUCKET_ENV = "GECKO_DATASETS_BUCKET"
DATASETS_KMS_KEY_ENV = "GECKO_DATASETS_KMS_KEY"
#: S3 bucket naming (lower case, digits, dots, hyphens; 3..63; no leading/trailing dot).
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_KMS_KEY = re.compile(
    r"^(arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(key|alias)/[A-Za-z0-9/_-]+"
    r"|alias/[A-Za-z0-9/_-]+"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_TRUE = frozenset({"1", "true", "yes", "on"})


class ConfigError(Exception):
    """A refused configuration. Names the variable, never echoes its value."""


@dataclass(frozen=True)
class AnyQueryConfig:
    billing: Literal["fake", "solana"] = "fake"  # GECKO_ANYQUERY_BILLING
    fee_wallet: str | None = None  # GECKO_FEE_WALLET; required with solana
    #: GECKO_RELAY_FEE_PAYER; required with solana. The rebate address, and nothing else.
    relay_fee_payer: str | None = None
    #: GECKO_RELAY_LOW_BELOW_LAMPORTS. Below this the founder is reminded to top the relay
    #: up. 0.01 SOL (~$1).
    relay_low_below_lamports: int = 10_000_000
    #: GECKO_RELAY_SWEEP_ABOVE_LAMPORTS. The hot-balance ceiling: above it the founder
    #: sweeps to cold by hand. 0.1 SOL (~$11), replacing 0.5
    #: and then 0.05.
    relay_sweep_above_lamports: int = 100_000_000
    rebate_split_user_pct: int | None = (
        None  # GECKO_REBATE_SPLIT_USER_PCT; required with solana
    )
    #: GECKO_ANYQUERY_MARGIN_PCT (0..100). The fee is max(floor, margin_pct of the data).
    gecko_margin_pct: int = 0
    #: GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO. A FLAT
    #: $0.10 per run on top of the data cost, so the default margin is 0.
    gecko_fee_floor_usdc_micro: int = 100_000
    max_run_usdc_micro: int = 5_000_000
    #: GECKO_ANYQUERY_MAX_RUN_LAMPORTS. The most SOL one signed transaction may move out
    #: of the user's wallet (a transfer to the treasury, or the wrap before a swap). It is
    #: written into the wallet's Privy policy at creation, so raising it later does not
    #: widen a wallet that already exists. 0.06 SOL is ~$6.5 at $109/SOL: room for the
    #: 5 USDC per-run ceiling plus slippage.
    max_run_sol_lamports: int = 60_000_000
    quote_ttl_s: int = 900
    max_query_chars: int = 2000
    max_subjects: int = 25
    max_fields: int = 8
    max_sources_per_field: int = 3
    swap_slippage_bps: int = 100
    wallet_create_enabled: bool = False  # GECKO_ANYQUERY_WALLET_CREATE
    #: GECKO_SETTLEMENT_WALLET_ID / GECKO_SETTLEMENT_WALLET: Gecko's own Privy server
    #: wallet that receives SOL payments and settles them. Both or neither.
    settlement_wallet_id: str | None = None
    settlement_wallet: str | None = None
    #: GECKO_SETTLEMENT_POOL: the ONE SOL/USDC Whirlpool SOL is priced on and swapped on.
    settlement_pool: str | None = None
    #: GECKO_SOL_PAYMENT_MARGIN_BPS: added to the pool price when a quote is paid in SOL,
    #: so the settlement swap's floor (after fee and slippage) still clears the quote.
    sol_payment_margin_bps: int = 200
    #: GECKO_SETTLEMENT_MAX_USDC_MICRO: the most USDC one settlement may forward to the
    #: treasury (the swap's floor plus any surplus left by earlier settlements).
    max_settlement_usdc_micro: int = 10_000_000
    #: GECKO_SETTLEMENT_WALLET_CREATE: the founder-run create, off by default.
    settlement_create_enabled: bool = False
    #: GECKO_SETTLEMENT_FLOAT_USDC_MICRO: the USDC a settlement leaves in the settlement
    #: wallet; only what it holds above this goes to the treasury (founder, 9 Oct).
    settlement_float_usdc_micro: int = 1_000_000
    #: GECKO_ORTHOGONAL_PAY: how Orthogonal calls are paid. ``key`` is unchanged.
    orthogonal_pay: OrthogonalPay = "key"
    #: GECKO_ORTHOGONAL_X402_PAYTO: an OVERRIDE, the one address every x402 payment
    #: goes to. Unset (the default), each operation's payTo is pinned from the service
    #: registry's last challenge probe: measured 9 Oct, Orthogonal's x402 host names a
    #: different payTo per provider, so one pin refused all but one.
    orthogonal_x402_pay_to: str | None = None
    #: GECKO_ORTHOGONAL_X402_MAX_CALL_USDC_MICRO: the most one x402 call may pay. Also
    #: written into the settlement wallet's Privy rule, so raising it means a policy update.
    max_x402_call_usdc_micro: int = 100_000
    #: GECKO_DATASETS_BUCKET / GECKO_DATASETS_KMS_KEY: where dataset snapshots go, and
    #: the KMS key every put names. Both or neither.
    datasets_bucket: str | None = None
    datasets_kms_key: str | None = None
    planner_model: str = ""  # set by ai-ml-engineer


def is_pubkey(text: str) -> bool:
    """True when ``text`` is base58 that decodes to exactly 32 bytes.

    Stdlib on purpose so config loads without the ``[solana]`` extra. A shape check, not
    a curve check: the fee wallet may legitimately be any 32-byte address.
    """
    if not isinstance(text, str) or not 32 <= len(text) <= 44:
        return False
    number = 0
    for char in text:
        digit = _B58.find(char)
        if digit < 0:
            return False
        number = number * 58 + digit
    leading = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return leading + len(body) == 32


def _int(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _settlement(env: Mapping[str, str], fee_wallet: str | None) -> dict[str, Any]:
    """The settlement wallet's settings, refused by name (never by value) when wrong."""
    wallet_id = env.get(SETTLEMENT_WALLET_ID_ENV, "").strip() or None
    wallet = env.get(SETTLEMENT_WALLET_ENV, "").strip() or None
    pool = env.get(SETTLEMENT_POOL_ENV, "").strip() or None
    if wallet_id is not None and not _PRIVY_ID.match(wallet_id):
        raise ConfigError(f"{SETTLEMENT_WALLET_ID_ENV} is not a Privy wallet id")
    if wallet is not None and not is_pubkey(wallet):
        raise ConfigError(f"{SETTLEMENT_WALLET_ENV} is not a valid Solana address")
    if (wallet_id is None) != (wallet is None):
        raise ConfigError(
            f"{SETTLEMENT_WALLET_ID_ENV} and {SETTLEMENT_WALLET_ENV} are set together "
            "(the id from the founder's create, and the address Privy answered with)"
        )
    if wallet is not None and wallet == fee_wallet:
        raise ConfigError(f"{SETTLEMENT_WALLET_ENV} must not be GECKO_FEE_WALLET")
    if pool is not None and not is_pubkey(pool):
        raise ConfigError(f"{SETTLEMENT_POOL_ENV} is not a valid Solana address")
    margin = _int(env, "GECKO_SOL_PAYMENT_MARGIN_BPS")
    if margin is not None and not 0 <= margin <= 2_000:
        raise ConfigError("GECKO_SOL_PAYMENT_MARGIN_BPS must be between 0 and 2000")
    cap = _int(env, "GECKO_SETTLEMENT_MAX_USDC_MICRO")
    if cap is not None and cap <= 0:
        raise ConfigError("GECKO_SETTLEMENT_MAX_USDC_MICRO must be positive")
    out: dict[str, Any] = {
        "settlement_wallet_id": wallet_id,
        "settlement_wallet": wallet,
        "settlement_pool": pool,
        "settlement_create_enabled": env.get("GECKO_SETTLEMENT_WALLET_CREATE", "")
        .strip()
        .lower()
        in _TRUE,
    }
    if margin is not None:
        out["sol_payment_margin_bps"] = margin
    if cap is not None:
        out["max_settlement_usdc_micro"] = cap
    float_micro = _int(env, "GECKO_SETTLEMENT_FLOAT_USDC_MICRO")
    if float_micro is not None:
        if float_micro < 0:
            raise ConfigError("GECKO_SETTLEMENT_FLOAT_USDC_MICRO must not be negative")
        out["settlement_float_usdc_micro"] = float_micro
    return out


def _orthogonal_pay(
    env: Mapping[str, str], settlement_wallet: str | None
) -> dict[str, Any]:
    """How Orthogonal calls are paid. ``x402`` pays from the settlement wallet, so it
    needs one; the payTo is refused by name when it is not an address."""
    mode = env.get(ORTHOGONAL_PAY_ENV, "").strip().lower() or "key"
    if mode not in ("key", "x402"):
        raise ConfigError(f"{ORTHOGONAL_PAY_ENV} must be 'key' or 'x402'")
    pay_to = env.get("GECKO_ORTHOGONAL_X402_PAYTO", "").strip() or None
    if pay_to is not None and not is_pubkey(pay_to):
        raise ConfigError("GECKO_ORTHOGONAL_X402_PAYTO is not a valid Solana address")
    if pay_to is not None and pay_to == settlement_wallet:
        raise ConfigError(
            "GECKO_ORTHOGONAL_X402_PAYTO must not be the settlement wallet"
        )
    cap = _int(env, "GECKO_ORTHOGONAL_X402_MAX_CALL_USDC_MICRO")
    if cap is not None and cap <= 0:
        raise ConfigError("GECKO_ORTHOGONAL_X402_MAX_CALL_USDC_MICRO must be positive")
    if mode == "x402" and settlement_wallet is None:
        raise ConfigError(
            f"{ORTHOGONAL_PAY_ENV}=x402 pays from the settlement wallet: set "
            f"{SETTLEMENT_WALLET_ID_ENV} and {SETTLEMENT_WALLET_ENV}"
        )
    out: dict[str, Any] = {"orthogonal_pay": mode, "orthogonal_x402_pay_to": pay_to}
    if cap is not None:
        out["max_x402_call_usdc_micro"] = cap
    return out


def _set_or_none(raw: str) -> str | None:
    """Empty and the SSM boot sentinel both mean unset."""
    value = raw.strip()
    return None if not value or value == PLATFORM_UNSET_SENTINEL else value


def _datasets(env: Mapping[str, str]) -> dict[str, Any]:
    """The snapshot bucket and its KMS key, refused by name (never by value)."""
    bucket = _set_or_none(env.get(DATASETS_BUCKET_ENV, ""))
    key = _set_or_none(env.get(DATASETS_KMS_KEY_ENV, ""))
    if bucket is not None and (not _BUCKET.match(bucket) or ".." in bucket):
        raise ConfigError(f"{DATASETS_BUCKET_ENV} is not an S3 bucket name")
    if key is not None and not _KMS_KEY.match(key):
        raise ConfigError(
            f"{DATASETS_KMS_KEY_ENV} is not a KMS key id (an ARN, alias/... or a UUID)"
        )
    if (bucket is None) != (key is None):
        raise ConfigError(
            f"{DATASETS_BUCKET_ENV} and {DATASETS_KMS_KEY_ENV} are set together: "
            "every snapshot put names the KMS key"
        )
    return {"datasets_bucket": bucket, "datasets_kms_key": key}


def _fee(env: Mapping[str, str]) -> dict[str, Any]:
    """Gecko's fee per run. The floor may not exceed the per-run ceiling, or every quote
    would be refused as over it."""
    out: dict[str, Any] = {}
    floor = _int(env, "GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO")
    if floor is not None:
        if not 0 <= floor <= AnyQueryConfig.max_run_usdc_micro:
            raise ConfigError(
                "GECKO_ANYQUERY_FEE_FLOOR_USDC_MICRO must be between 0 and "
                f"{AnyQueryConfig.max_run_usdc_micro}"
            )
        out["gecko_fee_floor_usdc_micro"] = floor
    margin = _int(env, "GECKO_ANYQUERY_MARGIN_PCT")
    if margin is not None:
        if not 0 <= margin <= 100:
            raise ConfigError("GECKO_ANYQUERY_MARGIN_PCT must be between 0 and 100")
        out["gecko_margin_pct"] = margin
    return out


def load_config(environ: Mapping[str, str] | None = None) -> AnyQueryConfig:
    """The config from ``environ`` (default ``os.environ``). Refuses a ``solana``
    config without a valid fee wallet and a 0..100 user split."""
    env = os.environ if environ is None else environ
    billing = env.get("GECKO_ANYQUERY_BILLING", "fake").strip() or "fake"
    if billing not in ("fake", "solana"):
        raise ConfigError("GECKO_ANYQUERY_BILLING must be 'fake' or 'solana'")
    fee_wallet = env.get("GECKO_FEE_WALLET", "").strip() or None
    relay = env.get(RELAY_FEE_PAYER_ENV, "").strip() or None
    split = _int(env, "GECKO_REBATE_SPLIT_USER_PCT")
    if fee_wallet is not None and not is_pubkey(fee_wallet):
        raise ConfigError("GECKO_FEE_WALLET is not a valid Solana address")
    if relay is not None and not is_pubkey(relay):
        raise ConfigError(f"{RELAY_FEE_PAYER_ENV} is not a valid Solana address")
    if split is not None and not 0 <= split <= 100:
        raise ConfigError("GECKO_REBATE_SPLIT_USER_PCT must be between 0 and 100")
    if billing == "solana":
        if fee_wallet is None:
            raise ConfigError("GECKO_FEE_WALLET is required when billing is solana")
        if split is None:
            raise ConfigError(
                "GECKO_REBATE_SPLIT_USER_PCT is required when billing is solana"
            )
        if relay is None:
            raise ConfigError(
                f"{RELAY_FEE_PAYER_ENV} is required when billing is solana"
            )
    low = _int(env, "GECKO_RELAY_LOW_BELOW_LAMPORTS")
    sweep = _int(env, "GECKO_RELAY_SWEEP_ABOVE_LAMPORTS")
    if low is not None and low < 0:
        raise ConfigError("GECKO_RELAY_LOW_BELOW_LAMPORTS must not be negative")
    if sweep is not None and sweep <= 0:
        raise ConfigError("GECKO_RELAY_SWEEP_ABOVE_LAMPORTS must be positive")
    create = env.get("GECKO_ANYQUERY_WALLET_CREATE", "").strip().lower() in _TRUE
    settlement = _settlement(env, fee_wallet)
    max_lamports = _int(env, "GECKO_ANYQUERY_MAX_RUN_LAMPORTS")
    if max_lamports is not None and max_lamports <= 0:
        raise ConfigError("GECKO_ANYQUERY_MAX_RUN_LAMPORTS must be positive")
    config = AnyQueryConfig(
        billing="solana" if billing == "solana" else "fake",
        fee_wallet=fee_wallet,
        relay_fee_payer=relay,
        rebate_split_user_pct=split,
        wallet_create_enabled=create,
        **settlement,
        **_orthogonal_pay(env, settlement["settlement_wallet"]),
        **_fee(env),
        **_datasets(env),
    )
    if low is not None:
        config = replace(config, relay_low_below_lamports=low)
    if sweep is not None:
        config = replace(config, relay_sweep_above_lamports=sweep)
    if config.relay_low_below_lamports >= config.relay_sweep_above_lamports:
        raise ConfigError(
            "GECKO_RELAY_LOW_BELOW_LAMPORTS must be below GECKO_RELAY_SWEEP_ABOVE_LAMPORTS"
        )
    if max_lamports is None:
        return config
    return replace(config, max_run_sol_lamports=max_lamports)
