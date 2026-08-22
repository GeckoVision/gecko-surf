"""Plan a change to a `let_me_buy` storefront — ordered unsigned steps, or a refusal.

WHY THIS IS NOT `prepare_instruction`. That tool builds ANY single instruction of ANY
comprehended program, and it is stateless: it cannot read a store, diff it against what you
want, notice that the result would exceed a cap, or order steps so none of them reverts.
Everything here is exactly that — the part a single call cannot see.

WHY IT LIVES IN `providers/` AND NOT THE ENGINE. Every rule below is this program's, not a
general truth: the twenty-product cap, the absence of any edit instruction, delete-before-
add on a name collision, byte-exact case-sensitive names. The engine stays agnostic of all
of it (architecture invariant #2); a storefront's rules belong beside the other program
surfaces.

WHY "PLAN" AND NOT "SYNC". It never signs, never sends, and never spins up a fork. It hands
back bytes and a verdict; a human or a founder-run script does the rest. The honesty of the
name is load-bearing when the thing on the other end is a live store with real history.

ADD-ONLY IS THE DEFAULT, AND THAT IS A SAFETY PROPERTY RATHER THAN A PREFERENCE. There is
no instruction that edits a product, so a price change is delete-then-add and a plan that
deletes first can strand a live store half-updated with no single-instruction recovery. So
`exact=False` (the default) only ever ADDS: extras are left alone and reported. Ask for
`exact=True` and you may get deletes — and the verdict will say plainly what that risks.

WHAT IT DOES NOT PROVE, stated because the omission is deliberate. The steps are built
unsigned and are NOT simulated one by one, because every step after the first would be
simulated against a state that does not exist yet — the second add against a store that
has not received the first. Simulating them anyway would produce a row of reassuring
green ticks that mean nothing. Sequence-level proof comes from running the plan on a fork
(`scripts/rehearse_store_showcase.py`); this tool's job is to refuse the plans that cannot
work at all, and to order the rest so they do not revert.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Sequence

from ..prepare_instruction import prepare_instruction_result
from ..rpc import RpcCall, default_rpc_call
from ..showcase import MAX_PRODUCTS
from ..store_accounts import receipts_pda
from ..store_directory import LET_ME_BUY_PROGRAM_ID, decode_store

__all__ = [
    "PlanStep",
    "PlanVerdict",
    "StorePlan",
    "plan_store_change",
    "plan_store_change_result",
]

Mode = Literal["add-only", "requires-deletes", "no-change"]


@dataclass(frozen=True)
class PlanStep:
    """One instruction of the plan, unsigned. `why` is the reason it is in the sequence."""

    index: int
    instruction: Literal["add_product", "delete_product"]
    product: str
    why: str
    values: Mapping[str, Any]
    transaction_base64: str | None = None
    build_refusal: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PlanVerdict:
    fits: bool
    mode: Mode
    #: True when executing PART of this plan leaves the store in a state no single
    #: instruction restores. Only deletes can cause it.
    stranding_risk: bool
    cap: str
    products_now: int
    products_after: int
    notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class StorePlan:
    store: str
    address: str
    authority: str
    verdict: PlanVerdict
    steps: tuple[PlanStep, ...] = field(default=())
    refused: str | None = None


def _read(call: RpcCall, rpc_url: str, address: str):  # noqa: ANN202
    value = (
        call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}]).get("result")
        or {}
    ).get("value") or {}
    if not value:
        raise LookupError(f"no let_me_buy store account at {address}")
    return decode_store(base64.b64decode(value["data"][0]), address=address)


def plan_store_change(
    store: str,
    target: Sequence[tuple[str, int]],
    *,
    rpc_url: str,
    exact: bool = False,
    idl_fetch: Callable[[str], dict[str, Any]] | None = None,
    build_call: Callable[..., Any] | None = None,
    rpc_call: RpcCall = default_rpc_call,
) -> StorePlan:
    """Diff the live store against `target` and order the steps so none of them reverts.

    `target` is `(name, price_lamports)` pairs. With `exact=False` they are items that must
    be PRESENT; with `exact=True` they are the whole intended menu and anything else is
    deleted.
    """
    address = receipts_pda(store)
    live = _read(rpc_call, rpc_url, address)
    have: dict[str, int] = {p.name: p.price_raw for p in live.products}
    want: dict[str, int] = dict(target)
    mint = live.products[0].mint if live.products else None

    # Byte-exact, case-sensitive — measured on a fork, where 'Sparkling Water' and
    # 'Sparkling water' were both accepted onto one store as separate products.
    additions = [(n, p) for n, p in target if n not in have]
    repricings = [(n, p) for n, p in target if n in have and have[n] != p]
    removals = [n for n in have if n not in want] if exact else []

    notes: list[str] = []
    if not exact and any(have.get(n) not in (None, p) for n, p in target):
        notes.append(
            f"{len(repricings)} target item(s) are on the store at a different price. "
            "add-only leaves them untouched — re-pricing needs exact=True, which deletes."
        )
        repricings = []
    extras = [n for n in have if n not in want]
    if not exact and extras:
        notes.append(
            f"{len(extras)} product(s) on the store are not in the target and are left "
            f"in place: {sorted(extras)}"
        )

    deletes = list(removals) + [n for n, _ in repricings]
    adds = list(additions) + list(repricings)
    after = len(have) + len(additions) - len(removals)

    verdict_mode: Mode = (
        "no-change"
        if not adds and not deletes
        else "add-only"
        if not deletes
        else "requires-deletes"
    )
    if deletes:
        notes.append(
            "THIS PLAN DELETES. `let_me_buy` has no instruction that edits a product, so "
            "if the sequence stops partway the store is left without the deleted items "
            "and without their replacements, and NO SINGLE INSTRUCTION restores it — only "
            "re-running the remaining adds. Prefer exact=False where the goal allows it."
        )

    if after > MAX_PRODUCTS:
        return StorePlan(
            store=store,
            address=address,
            authority=live.authority,
            verdict=PlanVerdict(
                fits=False,
                mode=verdict_mode,
                stranding_risk=bool(deletes),
                cap=f"{after}/{MAX_PRODUCTS}",
                products_now=len(have),
                products_after=after,
                notes=tuple(notes),
            ),
            refused=(
                f"the plan ends at {after} products and the program's cap is "
                f"{MAX_PRODUCTS}. add_product number {MAX_PRODUCTS + 1} reverts with "
                "VectorLimitReached (6008). The cap is a COUNT, not a byte budget — a "
                "store with every one of its 3,681 bytes free still refuses the 21st — so "
                "free space is not an argument for trying."
            ),
        )
    if adds and mint is None:
        return StorePlan(
            store=store,
            address=address,
            authority=live.authority,
            verdict=PlanVerdict(
                False,
                verdict_mode,
                bool(deletes),
                f"{after}/{MAX_PRODUCTS}",
                len(have),
                after,
                tuple(notes),
            ),
            refused=(
                "the store carries no product to read a pricing mint from, and this tool "
                "will not invent one — a mint is what a buyer pays in."
            ),
        )

    # ORDER: deletes first, and only because a colliding name must go before its
    # replacement can be added. Nothing else depends on order.
    steps: list[PlanStep] = []
    for name in deletes:
        steps.append(
            PlanStep(
                index=len(steps) + 1,
                instruction="delete_product",
                product=name,
                why=(
                    "removed: not in the target"
                    if name in removals
                    else "removed so it can be re-added at the new price"
                ),
                values={
                    "store_name": store,
                    "product_name": name,
                    "authority": live.authority,
                },
            )
        )
    for name, price in adds:
        steps.append(
            PlanStep(
                index=len(steps) + 1,
                instruction="add_product",
                product=name,
                why=(
                    "re-added at the new price"
                    if any(n == name for n, _ in repricings)
                    else "not on the store"
                ),
                values={
                    "store_name": store,
                    "name": name,
                    "price": price,
                    "mint": mint,
                    "authority": live.authority,
                },
            )
        )

    if idl_fetch is not None and build_call is not None:
        steps = [_build(step, live.authority, idl_fetch, build_call) for step in steps]

    return StorePlan(
        store=store,
        address=address,
        authority=live.authority,
        verdict=PlanVerdict(
            fits=True,
            mode=verdict_mode,
            stranding_risk=bool(deletes),
            cap=f"{after}/{MAX_PRODUCTS}",
            products_now=len(have),
            products_after=after,
            notes=tuple(notes),
        ),
        steps=tuple(steps),
    )


def _build(
    step: PlanStep,
    authority: str,
    idl_fetch: Callable[[str], dict[str, Any]],
    build_call: Callable[..., Any],
) -> PlanStep:
    """Unsigned bytes for one step. NOT simulated — see the module docstring."""
    prepared = prepare_instruction_result(
        {
            "program_id": LET_ME_BUY_PROGRAM_ID,
            "instruction": step.instruction,
            "payer": authority,
            "values": dict(step.values),
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
    )
    if prepared.get("refused"):
        return PlanStep(**{**step.__dict__, "build_refusal": prepared})
    return PlanStep(
        **{**step.__dict__, "transaction_base64": prepared.get("transaction_base64")}
    )


def plan_store_change_result(plan: StorePlan) -> dict[str, Any]:
    """The plan as a plain mapping, for a tool surface."""
    return {
        "refused": plan.refused is not None,
        "reason": plan.refused,
        "store": plan.store,
        "address": plan.address,
        "authority": plan.authority,
        "verdict": {
            "fits": plan.verdict.fits,
            "mode": plan.verdict.mode,
            "stranding_risk": plan.verdict.stranding_risk,
            "cap": plan.verdict.cap,
            "products_now": plan.verdict.products_now,
            "products_after": plan.verdict.products_after,
            "notes": list(plan.verdict.notes),
        },
        "steps": [
            {
                "index": s.index,
                "instruction": s.instruction,
                "product": s.product,
                "why": s.why,
                "values": dict(s.values),
                "transaction_base64": s.transaction_base64,
                "build_refusal": s.build_refusal,
            }
            for s in plan.steps
        ],
    }
