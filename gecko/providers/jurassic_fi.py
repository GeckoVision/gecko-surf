"""jurassic_fi (Jurassic Finance token sale) — the servable plan intent.

The find_start chain (:data:`gecko.find_start._CONTRIBUTE_THEN_CLAIM`) already routes
intents to ``contribute``/``claim`` and carries the program's honest gaps; this module
supplies the PLAN CALLABLE the served surface needs: given a launch and a contributor,
derive the accounts ``contribute`` must name and point at Orquestra's builder.

The launch root itself is the program's honest dead-end — seeded on two fields OF the
account being derived (['launch', launch.admin, launch.launch_id]) — so this intent
takes the launch ADDRESS as an input rather than pretending to derive it: the caller
knows which sale they are contributing to. Everything below the root derives.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..find_start import StartSpec
from ..landing import SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
from ..store_accounts import derive_ata
from .orquestra import Intent, OrquestraProgramSurface

__all__ = [
    "JURASSIC_FI_INTENTS",
    "JURASSIC_FI_STARTS",
]


def _contribute_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Derive the ``contribute`` account set below a known launch root.

    ``user_position`` and ``payment_vault`` derive from the packaged config's own
    recipes (:meth:`OrquestraProgramSurface.derive`); the contributor's payment ATA
    derives locally. The launch address is the caller's input — see the module
    docstring for why that is the honest contract, not a shortcut.
    """
    launch = str(args["launch"])
    contributor = str(args["contributor"])
    payment_mint = str(args["payment_mint"])
    return {
        "contributor": contributor,
        "launch": launch,
        "user_position": surface.derive(
            "user_position", {"launch": launch, "contributor": contributor}
        ),
        "payment_mint": payment_mint,
        "contributor_payment_account": derive_ata(
            contributor, payment_mint, token_program=TOKEN_PROGRAM_ID
        ),
        "payment_vault": surface.derive(
            "payment_vault",
            {
                "launch": launch,
                "token_program": TOKEN_PROGRAM_ID,
                "payment_mint": payment_mint,
            },
        ),
        "token_program": TOKEN_PROGRAM_ID,
        "system_program": SYSTEM_PROGRAM_ID,
    }


_PLAN_CONTRIBUTE = Intent(
    name="plan_contribute",
    instruction="contribute",
    description=(
        "Plan a Jurassic Finance sale contribution: derive every account "
        "`contribute` must name below a known launch — the per-contributor "
        "user_position (the program creates it on first contribute), the launch's "
        "payment vault, and the contributor's payment token account. Give launch "
        "(the sale's launch account address), contributor (the paying wallet) and "
        "payment_mint (the mint the launch is priced in — fixed by the launch, "
        "read it off the sale before paying). Amounts are builder arguments, in "
        "the payment mint's base units: requested_amount AND min_accepted_amount, "
        "because the program partially fills when headroom is short."
    ),
    inputs=("launch", "contributor", "payment_mint"),
    plan=_contribute_plan,
)


JURASSIC_FI_INTENTS: dict[str, Intent] = {_PLAN_CONTRIBUTE.name: _PLAN_CONTRIBUTE}

JURASSIC_FI_STARTS: dict[str, StartSpec] = {
    "plan_contribute": StartSpec(
        accounts=("launch", "user_position", "payment_vault"),
        recovered={
            "launch": (
                "seeded on ['launch', launch.admin, launch.launch_id] — two fields "
                "of the account being derived, so the surface cannot derive it from "
                "itself; the plan takes the address as an input instead of guessing"
            ),
            "user_position": (
                "['user_position', launch, contributor] — init_if_needed: the "
                "program creates it on first contribute, no separate init call"
            ),
            "payment_vault": (
                "the launch's OWN ATA for the payment mint [launch, token_program, "
                "payment_mint] — an account the IDL lists but never explains"
            ),
        },
    )
}
