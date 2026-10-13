"""The mint, read from the chain: supply, decimals, authorities, owner, extensions.

Two read-only calls through an ``rpc.RpcCall``: ``getTokenSupply`` and ``getAccountInfo``
with ``jsonParsed``. The token program is the account's OWNER (``token_program``'s one
rule: read, never inferred). The multiplier is the Token-2022 scaled-UI-amount
extension; the jsonParsed spelling ``scaledUiAmountConfig`` with ``multiplier``,
``newMultiplier`` and ``newMultiplierEffectiveTimestamp`` is the account decoder's
naming as recalled, not measured, so any extension whose name contains
``scaleduiamount`` is read and a missing number is "unread", never 1.

Absent and unread stay apart: an authority the chain reports as null is the fact
``(none)``; an authority we could not read is ``None``. A mint with no scaled-UI
extension has multiplier 1 by construction, and says so in a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ..netguard import UnsafeUrlError
from ..orthogonal.steps import PublicStep
from ..orthogonal.supplier.readers import field_names
from ..rpc import RpcCall, RpcError
from ..token_program import classify_token_program
from .xstocks import BASE58_ADDRESS

NO_AUTHORITY = "(none)"
_EXTENSION_NAME = re.compile(r"^[A-Za-z0-9]{1,64}$")
_INT = re.compile(r"^\d{1,40}$")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def decimal_of(value: Any) -> Decimal | None:
    """A finite, non-negative number from JSON (int, float or numeric string)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _authority(info: dict[str, Any], key: str) -> str | None:
    if key not in info:
        return None
    value = info[key]
    if value is None:
        return NO_AUTHORITY
    return value if isinstance(value, str) and BASE58_ADDRESS.match(value) else None


@dataclass
class MintRead:
    slot: int | None = None
    supply_raw: str | None = None
    #: The node's ``uiAmountString``. Whether a node applies the multiplier to it
    #: depends on its version, so the computed scaled supply is kept beside it.
    supply_ui_rpc: str | None = None
    decimals: int | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    token_program: str | None = None
    extensions: tuple[str, ...] | None = None
    multiplier: Decimal | None = None
    multiplier_note: str = ""
    steps: list[PublicStep] = field(default_factory=list)
    #: method -> our words for why it gave nothing.
    trouble: dict[str, str] = field(default_factory=dict)

    @property
    def token_program_name(self) -> str | None:
        return (
            classify_token_program(self.token_program) if self.token_program else None
        )

    def supply_scaled(self) -> Decimal | None:
        """raw / 10^decimals x multiplier: the UI supply a holder's balances add up to."""
        if self.supply_raw is None or self.decimals is None or self.multiplier is None:
            return None
        return Decimal(self.supply_raw).scaleb(-self.decimals) * self.multiplier


def _call(
    call: RpcCall, rpc_url: str, method: str, params: list[Any], read: MintRead
) -> dict[str, Any] | None:
    try:
        response = call(rpc_url, method, params)
    except RpcError:
        reason = "the node answered with a JSON-RPC error"
    except UnsafeUrlError:
        reason = "the RPC URL was refused by the network guard"
    except (OSError, ValueError):
        reason = "the node could not be reached"
    else:
        result = _dict(_dict(response).get("result"))
        read.steps.append(
            PublicStep(method, ("mint",), 200, tuple(field_names(result)), 1)
        )
        slot = _dict(result.get("context")).get("slot")
        if isinstance(slot, int) and not isinstance(slot, bool):
            read.slot = slot
        return result
    read.steps.append(PublicStep(method, ("mint",), None, (), 0, "unreachable"))
    read.trouble[method] = reason
    return None


def _read_supply(value: dict[str, Any], read: MintRead) -> None:
    amount, decimals = value.get("amount"), value.get("decimals")
    if isinstance(amount, str) and _INT.match(amount):
        read.supply_raw = amount
    if isinstance(decimals, int) and not isinstance(decimals, bool) and decimals < 30:
        read.decimals = decimals
    ui = decimal_of(value.get("uiAmountString"))
    read.supply_ui_rpc = format(ui, "f") if ui is not None else None
    if read.supply_raw is None:
        read.trouble["getTokenSupply"] = "the reply carried no integer amount"


def _read_multiplier(
    entries: list[dict[str, Any]], now_ts: float, read: MintRead
) -> None:
    scaled = [e for e in entries if "scaleduiamount" in str(e["extension"]).lower()]
    if not scaled:
        read.multiplier = Decimal(1)
        read.multiplier_note = "no scaled UI amount extension: balances are not rebased"
        return
    state = _dict(scaled[0].get("state"))
    current = decimal_of(state.get("multiplier"))
    pending = decimal_of(state.get("newMultiplier"))
    when = state.get("newMultiplierEffectiveTimestamp")
    if current is None or current == 0:
        read.multiplier_note = "the scaled UI amount extension carried no multiplier"
        return
    read.multiplier = current
    if pending is not None and pending != 0 and isinstance(when, int) and when > 0:
        if when <= now_ts:
            read.multiplier = pending
            read.multiplier_note = f"new multiplier in effect since unix {when}"
        elif pending != current:
            read.multiplier_note = f"multiplier {pending} takes effect at unix {when}"


def _read_account(value: dict[str, Any], now_ts: float, read: MintRead) -> None:
    owner = value.get("owner")
    if isinstance(owner, str) and BASE58_ADDRESS.match(owner):
        read.token_program = owner
    parsed = _dict(_dict(value.get("data")).get("parsed"))
    info = _dict(parsed.get("info"))
    if parsed.get("type") != "mint" or not info:
        read.trouble["getAccountInfo"] = "the account is not a parsed mint"
        return
    read.mint_authority = _authority(info, "mintAuthority")
    read.freeze_authority = _authority(info, "freezeAuthority")
    if read.decimals is None and isinstance(info.get("decimals"), int):
        read.decimals = int(info["decimals"])
    raw = info.get("extensions", [])
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    if not isinstance(raw, list) or len(entries) != len(raw):
        read.trouble["getAccountInfo"] = "the extension list could not be read"
        return
    names = [e.get("extension") for e in entries]
    if not all(isinstance(n, str) and _EXTENSION_NAME.match(n) for n in names):
        read.trouble["getAccountInfo"] = "an extension name could not be read"
        return
    read.extensions = tuple(str(n) for n in names)
    _read_multiplier(entries, now_ts, read)


def read_mint(rpc_url: str, mint: str, call: RpcCall, *, now_ts: float) -> MintRead:
    read = MintRead()
    supply = _call(call, rpc_url, "getTokenSupply", [mint], read)
    if supply is not None:
        _read_supply(_dict(supply.get("value")), read)
    account = _call(
        call, rpc_url, "getAccountInfo", [mint, {"encoding": "jsonParsed"}], read
    )
    if account is not None:
        value = account.get("value")
        if isinstance(value, dict):
            _read_account(value, now_ts, read)
        else:
            read.trouble["getAccountInfo"] = "no account exists at this mint"
    return read
