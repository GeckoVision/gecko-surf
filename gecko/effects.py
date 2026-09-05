"""What this transaction will do, said before anybody signs it.

THE GAP THIS FILLS. Every signer in reach signs the bytes it is handed. A hardware
wallet with no screen blinks an LED and signs; a keychain MCP decodes base64 and signs;
a paymaster checks the transaction against the *operator's* allowlist and co-signs. Each
of them establishes that somebody was PRESENT, never that the somebody AGREED to this
particular movement of money. ``prepare_purchase`` says so in as many words:

    "The receipt says these exact bytes would land against the state observed at that
     slot — it does NOT say this is the purchase you meant."

This module is the beginning of an answer to *"is this the purchase you meant"*. It
composes two things that already exist and were never joined: the message decoded from
the same bytes :func:`gecko.txbind.message_binding` hashes, and the token movements the
simulation observed. Nothing here decodes, fetches, or computes anything new.

THREE RULES, and they are the whole design:

1. **Derived, never described.** Every field traces to bytes we hold or a simulation we
   ran. Nothing is taken from an agent's account of what it intended.
2. **Omission over estimation.** A value we cannot derive is absent from the summary. A
   plausible number in the sentence a human reads before signing is worse than a gap,
   because a gap prompts a question and a wrong number ends one.
3. **It inherits its receipt's standing.** The summary carries ``origin`` and
   ``observed_slot`` because a description of a simulated future is only as good as the
   simulation, and a reader must be able to tell an observation from an assertion.

WHAT THIS IS NOT. It is not authorization, and it does not decide anything: the spend
policy in :mod:`gecko.spend_policy` reads the same decoded message and is the thing that
refuses. This only renders. A transaction that carries no binding — v0 lookup tables,
whose account addresses are not in the bytes — gets no summary either, for the reason
:mod:`gecko.txbind` gives: two byte-identical messages can touch different accounts, so
any sentence written about one of them would be a guess wearing a decoder's authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .simulate import Receipt, TokenDeltaUnmeasurable
from .txbind import DecodedMessage

#: Lamports per SOL. Rendering only — no arithmetic here decides anything.
_LAMPORTS_PER_SOL = 1_000_000_000


@dataclass(frozen=True)
class TokenEffect:
    """One mint leaving one owner, in the mint's own units.

    ``ui`` is the rendered amount the reader sees; ``raw`` is what the program moves.
    Both are carried because a reader compares the first and a machine compares the
    second, and rounding the raw value away would make the two disagree.
    """

    mint: str
    owner: str
    raw: int
    ui: str
    decimals: int


@dataclass(frozen=True)
class Effects:
    """What the simulation observed this transaction doing.

    Every field is optional in the sense that matters: it is present when it was derived
    and absent when it was not. ``unmeasured`` names what could not be read, so a reader
    can tell "nothing moved" from "we could not tell whether anything moved" — the
    distinction :class:`~gecko.simulate.TokenDeltaReport` keeps and that a summary
    collapsing it would throw away.
    """

    fee_payer: str
    programs: tuple[str, ...]
    tokens_out: tuple[TokenEffect, ...] = ()
    sol_delta_lamports: int | None = None
    sol_delta_ui: str | None = None
    sol_delta_account: str | None = None
    compute_units: int | None = None
    observed_slot: int | None = None
    origin: str = "asserted"
    network: str | None = None
    unmeasured: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """The MCP-facing shape. Absent facts are omitted, never nulled.

        A ``null`` in a tool result reads as "the answer is nothing"; a missing key reads
        as "this was not established", which is the true statement.
        """
        out: dict[str, Any] = {
            "fee_payer": self.fee_payer,
            "programs": list(self.programs),
            "origin": self.origin,
        }
        if self.tokens_out:
            out["tokens_out"] = [
                {
                    "mint": effect.mint,
                    "owner": effect.owner,
                    "amount": effect.ui,
                    "amount_raw": str(effect.raw),
                    "decimals": effect.decimals,
                }
                for effect in self.tokens_out
            ]
        if self.sol_delta_lamports is not None:
            out["sol_delta"] = {
                "lamports": self.sol_delta_lamports,
                "sol": self.sol_delta_ui,
                **(
                    {"account": self.sol_delta_account}
                    if self.sol_delta_account
                    else {}
                ),
            }
        if self.compute_units is not None:
            out["compute_units"] = self.compute_units
        if self.observed_slot is not None:
            out["observed_slot"] = self.observed_slot
        if self.network is not None:
            out["network"] = self.network
        if self.unmeasured:
            out["unmeasured"] = list(self.unmeasured)
        return out


def _render_sol(lamports: int) -> str:
    """Lamports as SOL, exact. Never rounded — the reader is about to sign this."""
    sign = "-" if lamports < 0 else ""
    whole, rest = divmod(abs(lamports), _LAMPORTS_PER_SOL)
    return f"{sign}{whole}.{rest:09d}".rstrip("0").rstrip(".") or "0"


def describe_effects(decoded: DecodedMessage, receipt: Receipt) -> Effects:
    """Compose the decoded message and the receipt into what the transaction does.

    Both arguments are required and neither is optional-with-a-default, because a summary
    built from one of them is the half-answer this module exists to avoid: the message
    alone says what is *called*, the receipt alone says what *moved*, and only together
    do they say what this transaction does to whom.
    """
    unmeasured: list[str] = []

    # Programs, in call order, de-duplicated but never re-sorted: the order an
    # instruction list is written in is a fact about the transaction.
    seen: dict[str, None] = {}
    for instruction in decoded.instructions:
        seen.setdefault(instruction.program_id, None)

    tokens_out: tuple[TokenEffect, ...] = ()
    report = receipt.token_delta
    if report is None:
        # The node returned no token balances at all — distinct from a measured zero.
        unmeasured.append(
            "token movements: the simulation carried no token balances, so nothing is "
            "claimed about what moved"
        )
    else:
        try:
            tokens_out = tuple(
                TokenEffect(
                    mint=outflow.mint,
                    owner=outflow.owner,
                    raw=outflow.raw,
                    ui=outflow.ui,
                    decimals=outflow.decimals,
                )
                for outflow in report.outflows()
            )
        except TokenDeltaUnmeasurable as error:
            # Fails closed as a whole, exactly as the report does: one refused mint makes
            # the token leg unmeasurable, because nothing proves the refused one is not
            # the drain.
            unmeasured.append(f"token movements: {error}")

    sol_delta = receipt.sol_delta
    return Effects(
        fee_payer=decoded.fee_payer,
        programs=tuple(seen),
        tokens_out=tokens_out,
        sol_delta_lamports=sol_delta,
        sol_delta_ui=_render_sol(sol_delta) if sol_delta is not None else None,
        sol_delta_account=receipt.sol_delta_account,
        compute_units=receipt.units_consumed,
        observed_slot=receipt.observed_slot,
        origin=receipt.origin,
        network=receipt.network if receipt.network else None,
        unmeasured=tuple(unmeasured),
    )
