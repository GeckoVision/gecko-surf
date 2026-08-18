"""The round trip, asserted against the chain — OUR graph, HIS engine, ONE address.

For every instruction `let_me_buy` exposes this walks the whole path and lets each
side judge the other:

    ProgramGraph  ->  gecko.project.fdl.to_fdl  ->  HIS compile()  ->  HIS run()

and then asks the two questions neither half can answer alone:

  PDA       does the address HIS `resolve.pda@1` derived equal the address WE derive?
            This is the point of the exercise. A wrong-but-well-formed seed mapping —
            an IDL `{kind: "account"}` translated to the wrong FDL kind, a seed bound
            to the wrong slot, the `mark_as_delivered` seed that names an arg its own
            instruction does not have — produces a VALID base58 address, compiles
            clean, and simulates without complaint against somebody else's account.
            His stack cannot catch it: from inside the flow, a derived address is
            simply what the seeds say. The only witness is a second, independent
            derivation, which is what `gecko.store_accounts.receipts_pda` is.

  CU        does the chain charge what the chain has already been measured charging?
            The compute figures are read off mainnet, not off a doc. `mark_as_delivered`
            is the exception and is treated as one: its compute is a function of the
            bytes resident in the store's receipts vec, so it is asserted against the
            measured MODEL's band and never against a constant.

Nothing here signs and nothing is broadcast: two RPC reads per instruction (a blockhash
and an unsigned simulation), which is exactly what `solana.compose_transaction@1` does.

The storefront it walks is YOURS, so no wallet, store or channel appears in this file.
Point it at your own by writing `~/.gecko/catalog-ci.json` — the same file
`scripts/catalog_ci.py` reads:

    {
      "buyer":     "<pubkey that would pay>",
      "authority": "<pubkey that owns the store>",
      "store":     "<store name>",
      "telegram":  "@<channel update_telegram_channel would write>"
    }

Every key may be overridden by the matching `GECKO_CI_*` environment variable. It fails
closed: an absent config is a refusal, never a default store.

    uv run python -m scripts.roundtrip_let_me_buy
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from gecko.program_graph import ProgramGraph, build_program_graph
from gecko.project.fdl import FdlProjectionError, to_fdl
from gecko.store_accounts import derive_ata, receipts_pda
from scripts.orquestra_bridge import (
    BridgeError,
    CompileOutcome,
    ProjectIdl,
    RunOutcome,
    compile_fdl,
    let_me_buy_project,
    run_fdl,
)

CONFIG_PATH = Path(
    os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
).expanduser()

#: A public mainnet node is enough — every call is a read.
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

#: Universal SPL addresses: identical for every caller, so they stay inline.
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: What the CHAIN charged, measured on mainnet. Not predictions — prior observations
#: this run has to reproduce. `mark_as_delivered` is deliberately absent: see MODEL.
CHAIN_VERIFIED_CU: Mapping[str, int] = {
    "make_purchase": 36399,
    "update_details": 25476,
    "update_telegram_channel": 25473,
    "add_product": 27039,
    "delete_product": 26015,
}

#: The one instruction whose compute is not a constant. It scans the receipts vec, so
#: its cost tracks the bytes resident there and MOVES as the store trades — pinning it
#: to a number would manufacture a failure every time somebody buys a drink.
#: `CU ~= 19,780 + 12.7 x resident receipt bytes`, measured across two stores.
DRIFTING = "mark_as_delivered"
MODEL_INTERCEPT_CU = 19_780.0
MODEL_CU_PER_BYTE = 12.7
#: WHY THE BAND IS WIDE, and it is a fact about the MODEL rather than about the run.
#: The published relation was fitted on `make_purchase` and cross-validates to 0.9% —
#: on `make_purchase`. Applied to `mark_as_delivered` it runs ~7% high, and that
#: residual is structural, not noise: two mainnet stores measured on 2026-08-17 differ
#: by 3 receipts / 97 resident bytes and by 3,313 CU, i.e. 34 CU per byte, 2.7x the
#: published 12.7 — no byte-linear relation with slope 12.7 passes through both. Re-read
#: per RECEIPT the same two points are collinear at ~1,104 CU each, which is the shape
#: Anchor's `Vec<Receipt>` deserialization actually has (fixed per-element struct work
#: dominates the few bytes of a product name). So the seed to fix is the model's
#: independent variable, not this band. Until that is re-fitted the band stays at ±10%:
#: tight enough that a changed relationship breaks it, loose enough that a store simply
#: trading does not.
MODEL_BAND = 0.10

#: Instructions with no chain-verified figure — the lifecycle's two ends, SIMULATED
#: only, so what they demonstrate here is the PDA assertion. Both were measured on
#: 2026-08-17 and neither behaves the way the surface's own notes say:
#:
#:   initialize    refused, and the refusal is corroborating evidence — the system
#:                 program's `Allocate: account <addr> already in use` names the exact
#:                 address our deriver produced, from outside the flow that derived it.
#:   delete_store  SUCCEEDED at 16,893 CU against a store that still lists two products
#:                 and holds 20 receipts. `gecko/providers/configs/orquestra/
#:                 let_me_buy.json` says it "refuses a store that still has products
#:                 (6005 StoreNotEmpty) — delete_product first". 6005 is in the IDL's
#:                 error table; the deployed program did not raise it. Nothing was
#:                 broadcast, so the store is intact — but the guardrail we publish is
#:                 not there, and a flow that trusts it deletes a live storefront.
NO_CU_GROUND_TRUTH = frozenset({"initialize", "delete_store"})

#: Printed under a row whose result needs a sentence to be read correctly. Kept next to
#: the constant it explains so the table can never quietly stop saying it.
LIFECYCLE_NOTES: Mapping[str, str] = {
    "initialize": (
        "expected refusal: the store already exists. The system program's log names "
        "the SAME address our deriver produced — the refusal corroborates the PDA."
    ),
    "delete_store": (
        "NO ground truth, and it succeeded — on a store that still lists products. "
        "Our own surface notes claim 6005 StoreNotEmpty guards this. It did not fire."
    ),
}

_ACCOUNT_DISCRIMINATOR_BYTES = 8


class RoundTripError(RuntimeError):
    """The round trip could not be attempted — config, transport, or a missing input."""


class ConfigError(RoundTripError):
    """The local storefront config is missing or incomplete. Never guesses a wallet."""


class ExpectationError(RoundTripError):
    """The comparison lost track of what it was supposed to check."""


class MissingInputError(RoundTripError):
    """A projected flow declares an input this script has no value for.

    Fails closed on purpose: a value invented here would be a value the chain then
    judges, and "the probe made something up" is indistinguishable from a defect in
    the surface until somebody re-reads both. That mistake has already been made once
    in this repo (see `scripts/catalog_ci.py`'s `delete_product` note).
    """


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def load_config() -> dict[str, str]:
    """Read the local storefront identities. Fails closed — never guesses a wallet."""
    stored: dict[str, str] = {}
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
    resolved = {
        key: os.environ.get(f"GECKO_CI_{key.upper()}") or stored.get(key, "")
        for key in ("buyer", "authority", "store", "telegram")
    }
    missing = sorted(key for key, value in resolved.items() if not value)
    if missing:
        raise ConfigError(
            f"missing {', '.join(missing)} — write {CONFIG_PATH} or set "
            f"{', '.join('GECKO_CI_' + key.upper() for key in missing)}. "
            "See this module's docstring for the shape."
        )
    return resolved


# ---------------------------------------------------------------------------
# the two assertions, as data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    """What this run must reproduce, from outside the flow that produces it.

    `addresses` is our own derivation of every PDA slot; `compute_units` is what the
    chain has been measured charging, or `None` where compute is not a constant.
    """

    addresses: Mapping[str, str]
    compute_units: int | None
    compute_band: tuple[int, int] | None = None
    #: How many PDA slots this instruction MUST have compared. Checking fewer is a
    #: failure, not a smaller pass: the guard is only a guard if it is unreachable
    #: when the property is false, and a comparison of nothing is always true.
    required_slots: int = 0


@dataclass(frozen=True)
class Row:
    """One instruction's scorecard. Every field is measured, none is inferred."""

    instruction: str
    compiled: bool
    ran: bool
    simulated: bool
    pdas_checked: int = 0
    pdas_matched: int = 0
    compute_units: int | None = None
    expected: Expectation | None = None
    mismatches: tuple[str, ...] = ()
    note: str | None = None

    @property
    def pda_ok(self) -> bool | None:
        required = self.expected.required_slots if self.expected else 0
        if self.pdas_checked < required:
            return False  # the comparison went missing; that is a failure, not a skip
        if not self.pdas_checked:
            return None if not required else False
        return self.pdas_matched == self.pdas_checked

    @property
    def cu_verdict(self) -> str:
        expected = self.expected
        if self.compute_units is None:
            return "—"
        if expected is None or (
            expected.compute_units is None and expected.compute_band is None
        ):
            return "n/a"
        if expected.compute_band is not None:
            low, high = expected.compute_band
            return "in band" if low <= self.compute_units <= high else "OUT OF BAND"
        return "exact" if self.compute_units == expected.compute_units else "MISMATCH"

    @property
    def cu_asserted(self) -> bool | None:
        verdict = self.cu_verdict
        if verdict in {"—", "n/a"}:
            return None
        return verdict in {"exact", "in band"}


# ---------------------------------------------------------------------------
# the cases — values only; the SHAPE comes from the graph
# ---------------------------------------------------------------------------


def input_values(config: Mapping[str, str], project: ProjectIdl) -> dict[str, Any]:
    """Every flow-input name this catalog entry can ask for -> a real value.

    Keyed by input NAME rather than by instruction: the projector derives each flow's
    inputs from the graph, so a hand-written per-instruction account list — the thing
    that scored a surface red for a defect in its own author — has nowhere to live.
    """
    return {
        "projectId": project.project_id,
        "signer": config["buyer"],
        "authority": config["authority"],
        "mint": USDC,
        "store_name": config["store"],
        # mark_as_delivered spells the same value with a leading underscore. Anchor
        # copies Rust's unused-parameter name into the IDL while the seed keeps the
        # original, which is the join this whole exercise exists to preserve.
        "_store_name": config["store"],
        "product_name": os.environ.get("GECKO_CI_PRODUCT", "Water"),
        "name": os.environ.get("GECKO_CI_NEW_PRODUCT", "Sparkling"),
        "price": 120_000,
        "table_number": 9,
        "details": "open until late",
        "telegram_channel_id": config["telegram"],
        "receipt_id": int(os.environ.get("GECKO_CI_RECEIPT_ID", "106")),
    }


def instruction_names(graph: ProgramGraph, instruction: str) -> frozenset[str]:
    """Every name the instruction itself declares: its args and its account slots.

    Read off the graph, not off the projected document, so it can audit that document.
    """
    for ix in graph.instructions:
        if ix.name == instruction:
            return frozenset(
                [name for name, _ in ix.args] + [a.name for a in ix.accounts]
            )
    raise ExpectationError(f"instruction {instruction!r} is not in the program graph")


def bind_inputs(
    instruction: str,
    declared: Sequence[str],
    values: Mapping[str, Any],
    *,
    belongs_to: frozenset[str],
) -> dict[str, Any]:
    """Fill the flow's declared inputs. An unknown name raises rather than defaults.

    `belongs_to` is every name the INSTRUCTION itself declares — its args and its
    account slots — read off the graph. An input outside that set is a name the
    projector invented, and this refuses it even when a value happens to be available.
    That is not hypothetical: with seed-alias recovery disabled the projector emits
    `$inputs.store_name` for `mark_as_delivered`, whose own args are `_store_name` and
    `receipt_id`. Because this script knows both spellings of the store name, the
    invented input bound, the address came out RIGHT, and the run reported PDA ok — the
    projector doing exactly what its own refusal text forbids, in green. The address
    being correct is precisely why nothing downstream can catch it; the defect is that
    the surface asked a caller for a value it was supposed to derive.
    """
    invented = sorted(set(declared) - belongs_to - {"projectId"})
    if invented:
        raise MissingInputError(
            f"the projected {instruction!r} flow declares input(s) {invented}, which "
            f"the instruction itself does not have. Its args and accounts are "
            f"{sorted(belongs_to)}. A projector that invents an input name has stopped "
            "deriving and started asking — refusing before a value can hide it."
        )
    bound: dict[str, Any] = {}
    for name in declared:
        if name not in values:
            raise MissingInputError(
                f"the projected {instruction!r} flow declares input {name!r} and this "
                "script has no value for it. Add one to `input_values` — inventing a "
                "value here would let the chain judge the probe's guess and report it "
                "as a defect in the surface."
            )
        bound[name] = values[name]
    return bound


#: The slots this script can derive for itself, stated ONCE and independently of the
#: dict that derives them. It is the floor the PDA comparison is held to: without it the
#: comparison has no idea how many slots it was supposed to check, and two separate
#: mutations proved that a scorecard reporting "0/0 matched" prints as a pass. Renaming
#: the projector's node-id prefix disabled every check and exited 0; typo'ing one key in
#: `ours` dropped `receipts` — the PDA in 8 of 8 instructions — and still exited 0.
DERIVABLE_SLOTS = frozenset(
    {"receipts", "sender_token_account", "recipient_token_account"}
)


def expectations(
    graph: ProgramGraph, config: Mapping[str, str], resident_receipt_bytes: int
) -> dict[str, Expectation]:
    """What each instruction must reproduce, derived WITHOUT the flow.

    Every address here comes from Gecko's own deriver (`receipts_pda` / `derive_ata`),
    which shares no code with `resolve.pda@1`. That independence is the entire value of
    the comparison: two implementations of the same recipe agreeing is evidence, one
    implementation agreeing with itself is not.
    """
    ours = {
        "receipts": receipts_pda(config["store"]),
        "sender_token_account": derive_ata(config["buyer"], USDC),
        "recipient_token_account": derive_ata(config["authority"], USDC),
    }
    if set(ours) != DERIVABLE_SLOTS:
        raise ExpectationError(
            f"`ours` derives {sorted(ours)} but DERIVABLE_SLOTS names "
            f"{sorted(DERIVABLE_SLOTS)}. A slot that silently leaves this dict stops "
            "being compared while the summary still reads as a pass."
        )
    predicted = MODEL_INTERCEPT_CU + MODEL_CU_PER_BYTE * resident_receipt_bytes
    band = (
        int(round(predicted * (1 - MODEL_BAND))),
        int(round(predicted * (1 + MODEL_BAND))),
    )
    out: dict[str, Expectation] = {}
    for ix in graph.instructions:
        slots = {a.name: ours[a.name] for a in ix.accounts if a.name in ours}
        # The floor comes from the GRAPH's account list, never from `slots` — a floor
        # derived from the same dict it audits drops whenever that dict does.
        required = sum(1 for a in ix.accounts if a.name in DERIVABLE_SLOTS)
        if ix.name == DRIFTING:
            out[ix.name] = Expectation(
                slots, compute_units=None, compute_band=band, required_slots=required
            )
        else:
            out[ix.name] = Expectation(
                slots, CHAIN_VERIFIED_CU.get(ix.name), required_slots=required
            )
    return out


# ---------------------------------------------------------------------------
# the one chain read this script makes for itself
# ---------------------------------------------------------------------------


def resident_receipt_bytes(raw: bytes) -> int:
    """How many bytes of the store account the receipts vec currently occupies.

    Walked field by field, exactly as `gecko.store_directory.decode_store` does, and
    nothing is returned but a COUNT: no buyer, no price, no timestamp leaves this
    function. Anchor writes the vec in place and never zeroes the tail, so the account
    LENGTH (a constant 3,681 bytes) says nothing about what is resident — only the
    walk does, and the walk is what the program's own scan pays for.
    """
    cursor = _ACCOUNT_DISCRIMINATOR_BYTES
    rows = int.from_bytes(raw[cursor : cursor + 4], "little")
    cursor += 4
    start = cursor
    for _ in range(rows):
        cursor += 8 + 32 + 1 + 8 + 8 + 1  # id, buyer, delivered, price, ts, table
        length = int.from_bytes(raw[cursor : cursor + 4], "little")
        cursor += 4 + length  # product_name
        if cursor > len(raw):
            raise RoundTripError(
                "the store account's receipts vec runs past the account — refusing to "
                "model compute from bytes that were never read"
            )
    return cursor - start


def read_resident_receipt_bytes(store_address: str, *, rpc_url: str) -> int:
    """One targeted `getAccountInfo` on the store's own PDA. Read, never persisted."""
    import base64

    from gecko.rpc import default_rpc_call

    response = default_rpc_call(
        rpc_url, "getAccountInfo", [store_address, {"encoding": "base64"}]
    )
    value = (response.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise RoundTripError(
            f"the store account {store_address} does not exist on this network — "
            "there is nothing to round-trip against"
        )
    data = value.get("data")
    payload = data[0] if isinstance(data, list) and data else data
    if not isinstance(payload, str):
        raise RoundTripError("the node returned no base64 account data")
    return resident_receipt_bytes(base64.b64decode(payload))


# ---------------------------------------------------------------------------
# one instruction, all the way round
# ---------------------------------------------------------------------------


def check_pdas(
    outcome: RunOutcome, expected: Expectation
) -> tuple[int, int, list[str]]:
    """Compare every address HIS engine derived against OUR own derivation.

    A slot we cannot independently derive is not counted as a pass — it is not counted
    at all. Scoring an unchecked thing green is how a scorecard starts lying.
    """
    checked = 0
    matched = 0
    mismatches: list[str] = []
    for node_id, output in outcome.node_outputs.items():
        if not node_id.startswith("pda_") or not isinstance(output, Mapping):
            continue
        slot = node_id[len("pda_") :]
        ours = expected.addresses.get(slot)
        if ours is None:
            continue
        checked += 1
        theirs = output.get("address")
        if theirs == ours:
            matched += 1
        else:
            mismatches.append(f"{slot}: his {theirs} != ours {ours}")
    return checked, matched, mismatches


def round_trip(
    graph: ProgramGraph,
    instruction: str,
    *,
    values: Mapping[str, Any],
    expected: Expectation,
    project: ProjectIdl,
    rpc_url: str,
) -> Row:
    """Project, compile, run, and judge one instruction."""
    try:
        doc = to_fdl(
            graph,
            instruction,
            slug=f"let-me-buy-{instruction.replace('_', '-')}",
            intent=f"{instruction.replace('_', ' ')} on a let_me_buy storefront",
        )
    except FdlProjectionError as exc:
        return Row(instruction, False, False, False, note=f"refused: {exc}")

    compiled: CompileOutcome = compile_fdl(doc)
    if not compiled.ok:
        return Row(
            instruction, False, False, False, note="; ".join(compiled.messages)[:120]
        )

    outcome = run_fdl(
        doc,
        bind_inputs(
            instruction,
            list(doc["inputs"]),
            values,
            belongs_to=instruction_names(graph, instruction),
        ),
        rpc_url=rpc_url,
        projects=[project],
    )
    checked, matched, mismatches = check_pdas(outcome, expected)
    if not outcome.ok:
        return Row(
            instruction,
            True,
            False,
            False,
            pdas_checked=checked,
            pdas_matched=matched,
            expected=expected,
            mismatches=tuple(mismatches),
            note=(outcome.error.message if outcome.error else "run failed")[:120],
        )

    transaction = outcome.node_outputs["tx"]["transactions"][0]
    simulation = transaction["simulation"]
    return Row(
        instruction,
        True,
        True,
        bool(simulation["success"]),
        pdas_checked=checked,
        pdas_matched=matched,
        compute_units=simulation.get("computeUnits"),
        expected=expected,
        mismatches=tuple(mismatches),
        note=None if simulation["success"] else _failure_note(simulation),
    )


#: Markers for the log line that says WHY. The first match wins, not the last: a
#: failure cascades outward (`Program … failed: custom program error: 0x0` is every
#: refusal's last line and identifies none of them), so the root cause is the earliest
#: line that mentions one — `Allocate: account … already in use`, or Anchor's
#: `AnchorError caused by account: …`.
_FAILURE_MARKERS = ("Error", "already in use", "insufficient", "failed:")


def _failure_note(simulation: Mapping[str, Any]) -> str:
    """The chain's own words for a refused simulation — its error, and why."""
    err = simulation.get("err")
    reasons = [
        line
        for line in (simulation.get("logs") or [])
        if any(marker in line for marker in _FAILURE_MARKERS)
    ]
    return (json.dumps(err) if err else "simulation failed") + (
        f" | {reasons[0]}" if reasons else ""
    )


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"{'instruction':26} {'compile':7} {'run':5} {'sim':5} "
            f"{'pda':>9} {'cu':>7} {'expected':>10} {'cu check':12}",
            "─" * 92,
        ]
        for row in self.rows:
            expected = row.expected
            if expected is None or (
                expected.compute_units is None and expected.compute_band is None
            ):
                expected_cell = "—"
            elif expected.compute_band is not None:
                expected_cell = f"{expected.compute_band[0]}-{expected.compute_band[1]}"
            else:
                expected_cell = str(expected.compute_units)
            pda_cell = (
                f"{row.pdas_matched}/{row.pdas_checked} ok"
                if row.pda_ok
                else (
                    f"{row.pdas_matched}/{row.pdas_checked} BAD"
                    if row.pdas_checked
                    else "—"
                )
            )
            lines.append(
                f"{row.instruction:26} {str(row.compiled):7} {str(row.ran):5} "
                f"{str(row.simulated):5} {pda_cell:>9} "
                f"{str(row.compute_units if row.compute_units is not None else '—'):>7} "
                f"{expected_cell:>10} {row.cu_verdict:12}"
            )
            for mismatch in row.mismatches:
                lines.append(f"{'':26} └─ PDA {mismatch}")
            if row.note:
                lines.append(f"{'':26} └─ {row.note}")
            lifecycle = LIFECYCLE_NOTES.get(row.instruction)
            if lifecycle:
                lines.append(f"{'':26} └─ {lifecycle}")

        checked = [r for r in self.rows if r.pdas_checked]
        cu_rows = [r for r in self.rows if r.cu_asserted is not None]
        lines += [
            "",
            f"  compiles (his compiler) : {sum(r.compiled for r in self.rows)}/{len(self.rows)}",
            f"  runs     (his engine)   : {sum(r.ran for r in self.rows)}/{len(self.rows)}",
            f"  simulates on mainnet    : {sum(r.simulated for r in self.rows)}/{len(self.rows)}",
            f"  PDA his == ours         : {sum(1 for r in checked if r.pda_ok)}/{len(checked)}"
            f"  ({sum(r.pdas_matched for r in checked)}/{sum(r.pdas_checked for r in checked)} accounts)",
            f"  compute as measured     : {sum(1 for r in cu_rows if r.cu_asserted)}/{len(cu_rows)}"
            f"  ({len(self.rows) - len(cu_rows)} with no chain-verified figure)",
        ]
        # A comparison that went missing must SAY so. Reporting "0/0" reads as a pass to
        # a human even when the exit code disagrees, and a scorecard whose headline can
        # be misread is the failure it exists to catch.
        short = [
            (r.instruction, r.expected.required_slots - r.pdas_checked)
            for r in self.rows
            if r.expected and r.pdas_checked < r.expected.required_slots
        ]
        if short:
            missing = ", ".join(f"{name} (-{n})" for name, n in short)
            lines.append(
                f"  !! PDA SLOTS NOT COMPARED : {missing} — the comparison went "
                "missing, which is a failure and not a smaller pass."
            )
        return "\n".join(lines)


def main() -> int:
    config = load_config()
    rpc_url = (
        os.environ.get("GECKO_ROUNDTRIP_RPC_URL")
        or os.environ.get("GECKO_BRIDGE_RPC_URL")
        or DEFAULT_RPC
    )
    project = let_me_buy_project()
    graph = build_program_graph(idl=dict(project.idl), program_id=project.program_id)
    store_address = receipts_pda(config["store"])
    resident = read_resident_receipt_bytes(store_address, rpc_url=rpc_url)
    expected = expectations(graph, config, resident)
    values = input_values(config, project)

    report = Report()
    for ix in sorted(graph.instructions, key=lambda i: i.name):
        report.rows.append(
            round_trip(
                graph,
                ix.name,
                values=values,
                expected=expected[ix.name],
                project=project,
                rpc_url=rpc_url,
            )
        )

    print(report.render())
    predicted = MODEL_INTERCEPT_CU + MODEL_CU_PER_BYTE * resident
    drifting = next((r for r in report.rows if r.instruction == DRIFTING), None)
    residual = (
        f" — observed {drifting.compute_units}, "
        f"{(drifting.compute_units - predicted) / predicted:+.1%} against the model"
        if drifting is not None and drifting.compute_units is not None
        else ""
    )
    print(
        f"\n  {DRIFTING} is not pinned: its compute scans the receipts vec, which holds "
        f"{resident} bytes\n  right now — model {MODEL_INTERCEPT_CU:.0f} + "
        f"{MODEL_CU_PER_BYTE} x {resident} = {predicted:.0f} CU, "
        f"band ±{MODEL_BAND:.0%}{residual}."
    )
    print(
        f"  {len(NO_CU_GROUND_TRUTH)} instructions carry no chain-verified figure: "
        f"{', '.join(sorted(NO_CU_GROUND_TRUTH))}."
    )
    print("  Nothing was signed and nothing was broadcast.")

    failed = [
        r
        for r in report.rows
        if r.pda_ok is False or r.cu_asserted is False or not r.compiled
    ]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RoundTripError, BridgeError) as exc:
        raise SystemExit(f"roundtrip refused: {exc}") from exc
