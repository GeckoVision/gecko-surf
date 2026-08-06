"""Jupiter — the swap that needs BOTH surfaces to exist.

This is the first flow in the repo that cannot be built from a program surface alone,
and it is the clearest statement of why the two Gecko lanes are one product.

Orquestra serves Jupiter's program surface faithfully: `route` declares **9 accounts and
0 PDA-derivable accounts**. The instruction that actually lands carries **25 accounts and
an address lookup table**. The missing 16 are the AMM legs — pool, vaults, oracles, tick
arrays — of whichever route the aggregator chose at that instant.

**No IDL can carry them.** Not a deficient IDL — *any* IDL. The accounts do not exist as
a fact about the program; they exist as a fact about a route that a different surface (an
HTTP quote API) computes fresh per request. An agent holding a perfect program surface
and a perfect OpenAPI spec still cannot make this call, because the joining fact lives
between them.

So the plan here is assembled from three origins, and every account says which:

* ``extracted``     — declared by the program surface (the 9)
* ``recovered``     — derived from program source where the surface dropped the seed
                      (``event_authority``: seeds ``["__event_authority"]``, reported as
                      ``pda: null``)
* ``cross_surface`` — supplied by the HTTP surface at request time (the route legs)

The join key is the **mint**, which is also how the peg oracle names the same asset — so
"this asset is drifting" and "here is the route out of it" are the same graph.

Control plane throughout: we read public quotes and public account metadata, never store
a payload, never sign, never broadcast. The bundle is simulated unsigned.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..landing import simulate_landing_bundle as _simulate_landing_bundle
from ..netguard import validate_public_url
from ..provenance import AccountProvenance
from ..rpc import RpcCall
from ..simulate import Receipt

JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
#: The aggregator's public quote/instruction surface — the OTHER half of this call.
JUPITER_HTTP_BASE = "https://lite-api.jup.ag/swap/v1"
#: Anchor's event-authority PDA. Orquestra reports `pda: null` for it (the #4057 class:
#: the seed is a const in source, not an IDL field), so we recover the recipe here.
EVENT_AUTHORITY_SEEDS = ("__event_authority",)

DEFAULT_SLIPPAGE_BPS = 50


class JupiterLandingError(Exception):
    """A Jupiter route could not be planned or simulated. Never carries a secret."""


@dataclass(frozen=True)
class PlannedAccount:
    """One account in the route, with the origin we can actually defend."""

    pubkey: str
    is_signer: bool
    is_writable: bool
    provenance: AccountProvenance


@dataclass(frozen=True)
class RouteLandingResult:
    """The two receipts plus the provenance breakdown that IS the finding."""

    landing_receipt: Receipt
    derive_only_receipt: Receipt | None
    accounts: tuple[PlannedAccount, ...]
    route_labels: tuple[str, ...]
    price_impact_pct: str
    in_amount: str
    out_amount: str
    lookup_tables: tuple[str, ...]
    unit_limit: int

    @property
    def provenance_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for account in self.accounts:
            counts[account.provenance] = counts.get(account.provenance, 0) + 1
        return counts


HttpGet = Callable[[str], Mapping[str, Any]]
HttpPost = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def _default_get(url: str) -> Mapping[str, Any]:
    validate_public_url(url)  # no SSRF: the aggregator host is still an untrusted input
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - validated
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise JupiterLandingError("quote surface returned a non-object payload")
    return payload


def _default_post(url: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    validate_public_url(url)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310 - validated
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise JupiterLandingError("swap surface returned a non-object payload")
    return payload


def quote_route(
    input_mint: str,
    output_mint: str,
    amount: int,
    *,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    http_get: HttpGet | None = None,
    base: str = JUPITER_HTTP_BASE,
) -> Mapping[str, Any]:
    """Ask the HTTP surface which venues can actually absorb this trade right now.

    This is the step a program surface cannot substitute for, and it is also the answer
    to the peg oracle's own warning that thin venues lie: the route comes back with a
    measured ``priceImpactPct``, so depth is a number rather than an assumption.
    """
    query = urllib.parse.urlencode(
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
    )
    quote = (http_get or _default_get)(f"{base}/quote?{query}")
    if quote.get("error") or not quote.get("routePlan"):
        raise JupiterLandingError(
            f"no route for {input_mint[:8]}…→{output_mint[:8]}… at this size"
        )
    return quote


def _label_provenance(
    pubkey: str, index: int, declared: Sequence[str]
) -> AccountProvenance:
    """Which surface can honestly claim this account.

    The program surface declares a fixed prefix of named accounts; the event authority is
    a PDA the surface drops the seeds for; everything past the declared set is a route leg
    that only the HTTP surface knows.
    """
    if pubkey == _event_authority():
        return "recovered"
    if index < len(declared):
        return "extracted"
    return "cross_surface"


def jupiter_instruction_to_solders(instruction: Mapping[str, Any]) -> Any:
    """Jupiter's instruction shape → solders.

    Not a duplicate of ``orquestra_instruction_to_solders``: the two surfaces disagree on
    the wire, which is the same lesson one level down. Orquestra sends instruction data as
    **hex**; Jupiter sends **base64**. Both are correct for their own API and neither
    declares the other's convention, so something has to hold both — that something is
    this layer.
    """
    import base64 as _b64

    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program_id = instruction.get("programId") or instruction.get("program_id")
    if not program_id:
        raise JupiterLandingError("instruction carries no programId")
    try:
        data = _b64.b64decode(str(instruction.get("data") or ""), validate=True)
    except Exception as exc:  # noqa: BLE001 - redact: never echo the payload
        raise JupiterLandingError("instruction data is not valid base64") from exc

    metas = [
        AccountMeta(
            pubkey=Pubkey.from_string(str(entry["pubkey"])),
            is_signer=bool(entry.get("isSigner")),
            is_writable=bool(entry.get("isWritable")),
        )
        for entry in instruction.get("accounts", [])
    ]
    return Instruction(Pubkey.from_string(str(program_id)), data, metas)


def _event_authority() -> str:
    """Derive Jupiter's event authority from the seed recipe the IDL omits."""
    from solders.pubkey import Pubkey

    address, _bump = Pubkey.find_program_address(
        [seed.encode("utf-8") for seed in EVENT_AUTHORITY_SEEDS],
        Pubkey.from_string(JUPITER_PROGRAM),
    )
    return str(address)


#: The account names Orquestra's program surface declares for `route` — the honest
#: baseline we measure the gap against (fetched live in the E2E; pinned here so the
#: offline path is falsifiable without network).
DECLARED_ROUTE_ACCOUNTS: tuple[str, ...] = (
    "token_program",
    "user_transfer_authority",
    "user_source_token_account",
    "user_destination_token_account",
    "destination_token_account",
    "destination_mint",
    "platform_fee_account",
    "event_authority",
    "program",
)


def plan_route(
    bindings: Mapping[str, Any],
    *,
    http_get: HttpGet | None = None,
    http_post: HttpPost | None = None,
    base: str = JUPITER_HTTP_BASE,
) -> Mapping[str, Any]:
    """Intent → a complete, provenance-tagged Jupiter route plan.

    ``bindings`` needs ``input_mint``, ``output_mint``, ``amount``, ``user``.
    Returns the plan as structured data — no transaction is built or sent here.
    """
    required = ("input_mint", "output_mint", "amount", "user")
    missing = [key for key in required if not bindings.get(key)]
    if missing:
        raise JupiterLandingError(f"plan_route missing binding(s): {missing}")

    quote = quote_route(
        str(bindings["input_mint"]),
        str(bindings["output_mint"]),
        int(bindings["amount"]),
        slippage_bps=int(bindings.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
        http_get=http_get,
        base=base,
    )
    built = (http_post or _default_post)(
        f"{base}/swap-instructions",
        {
            "userPublicKey": str(bindings["user"]),
            "quoteResponse": quote,
            "wrapAndUnwrapSol": True,
        },
    )
    if built.get("error"):
        raise JupiterLandingError(f"swap surface refused the route: {built['error']}")
    swap_ix = built.get("swapInstruction")
    if not isinstance(swap_ix, dict):
        raise JupiterLandingError("swap surface returned no swapInstruction")

    accounts = tuple(
        PlannedAccount(
            pubkey=str(entry["pubkey"]),
            is_signer=bool(entry.get("isSigner")),
            is_writable=bool(entry.get("isWritable")),
            provenance=_label_provenance(
                str(entry["pubkey"]), i, DECLARED_ROUTE_ACCOUNTS
            ),
        )
        for i, entry in enumerate(swap_ix.get("accounts", []))
    )
    hops = tuple(
        str(step.get("swapInfo", {}).get("label", "?"))
        for step in quote.get("routePlan", [])
    )
    return {
        "instruction": "route",
        "program": JUPITER_PROGRAM,
        "accounts": accounts,
        "route_labels": hops,
        "price_impact_pct": str(quote.get("priceImpactPct", "")),
        "in_amount": str(quote.get("inAmount", "")),
        "out_amount": str(quote.get("outAmount", "")),
        "lookup_tables": tuple(built.get("addressLookupTableAddresses") or ()),
        "swap_instruction": swap_ix,
        "setup_instructions": tuple(built.get("setupInstructions") or ()),
        "declared_account_count": len(DECLARED_ROUTE_ACCOUNTS),
    }


def simulate_route_landing(
    bindings: Mapping[str, Any],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    unit_price_microlamports: int = 0,
    include_derive_only: bool = True,
    http_get: HttpGet | None = None,
    http_post: HttpPost | None = None,
    base: str = JUPITER_HTTP_BASE,
    network_label: str | None = None,
    record_to: str | Path | None = None,
) -> RouteLandingResult:
    """Plan the route, assemble the bundle, and simulate it → a Receipt.

    ``include_derive_only`` also simulates the *program-surface-only* call — the 9
    declared accounts with the route legs withheld — so the two receipts are one honest
    run rather than two takes stitched together.
    """
    plan = plan_route(bindings, http_get=http_get, http_post=http_post, base=base)
    user = str(bindings["user"])

    swap_ix = jupiter_instruction_to_solders(plan["swap_instruction"])
    prelude = [jupiter_instruction_to_solders(ix) for ix in plan["setup_instructions"]]

    landing_receipt, unit_limit = _simulate_landing_bundle(
        swap_ix,
        prelude,
        user,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
        unit_price_microlamports=unit_price_microlamports,
        track=[user],
        network_label=network_label,
        program="jupiter",
        instruction="route",
    )

    derive_only_receipt: Receipt | None = None
    if include_derive_only:
        # What an agent gets from the program surface alone: the declared accounts, with
        # every cross-surface route leg withheld. It is not a fair fight and that is the
        # point — the surface is complete for what it describes and cannot describe this.
        stripped = dict(plan["swap_instruction"])
        stripped["accounts"] = list(plan["swap_instruction"]["accounts"])[
            : plan["declared_account_count"]
        ]
        try:
            derive_only_receipt, _ = _simulate_landing_bundle(
                jupiter_instruction_to_solders(stripped),
                [],
                user,
                rpc_url=rpc_url,
                rpc_call=rpc_call,
                network_label=network_label,
            )
        except Exception:  # noqa: BLE001 - a build that cannot even be assembled IS the finding
            derive_only_receipt = None

    result = RouteLandingResult(
        landing_receipt=landing_receipt,
        derive_only_receipt=derive_only_receipt,
        accounts=plan["accounts"],
        route_labels=plan["route_labels"],
        price_impact_pct=plan["price_impact_pct"],
        in_amount=plan["in_amount"],
        out_amount=plan["out_amount"],
        lookup_tables=plan["lookup_tables"],
        unit_limit=unit_limit,
    )

    if record_to is not None:
        from .landing_record import record_landing_outcome

        # Categorical only, same contract as every other orchestrator: status / revert
        # family / units / slot / network + a values-free recipe hash. The route legs are
        # deliberately NOT part of the fingerprint — they change per quote, and a hash
        # that moves every request cannot anchor a drift series.
        record_landing_outcome(
            landing_receipt,
            program_id=JUPITER_PROGRAM,
            instruction="route",
            account_names=DECLARED_ROUTE_ACCOUNTS,
            arg_names=("route_plan", "in_amount", "quoted_out_amount", "slippage_bps"),
            pdas={},
            network_label=network_label,
            rpc_url=rpc_url,
            rpc_call=rpc_call,
            record_to=record_to,
        )
    return result


__all__ = [
    "DECLARED_ROUTE_ACCOUNTS",
    "EVENT_AUTHORITY_SEEDS",
    "JUPITER_HTTP_BASE",
    "JUPITER_PROGRAM",
    "JupiterLandingError",
    "jupiter_instruction_to_solders",
    "PlannedAccount",
    "RouteLandingResult",
    "plan_route",
    "quote_route",
    "simulate_route_landing",
]
