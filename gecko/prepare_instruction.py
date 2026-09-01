"""Prepare ANY instruction of ANY comprehended program — unsigned, simulated, or refused.

``prepare_purchase`` does this for one instruction of one program. Everything specific to
that store lives in its plan; everything general lives here, so a second program is a
caller rather than a copy.

THE SEAM THIS IS BUILT ON, AND WHY IT IS THE RIGHT ONE. A catalogue that holds an IDL can
already *construct* an instruction — encode the arguments, fetch a blockhash, serialize.
What it cannot do is *derive the accounts*, because an Anchor IDL states a seed's path and
not its width, and states some seeds as fields of the very account being addressed. So the
division is: we derive, the builder builds. Measured on jurassic_fi `contribute` — our
accounts handed to a partner's own builder produced a transaction that simulates on
mainnet at 21,368 CU, bit-identical to one we built ourselves. The partner's construction
was never wrong; only the derivation was missing.

WHAT IT WILL NOT DO. It does not sign, it does not broadcast, and it does not guess. An
account it cannot derive is named, with what it would need, and the whole call is refused —
because a transaction missing one account fails at signing time, far from here, and a
transaction carrying a WRONG account is worse: it is well-formed, it may even land, and
nothing downstream catches it.

Every response is returned to the caller and never persisted (invariant #1).
"""

from __future__ import annotations

from .tools import tool_annotations

from typing import Any, Callable, Literal, Mapping

from .error_overlay import remediation_for
from .program_errors import name_program_error
from .pda import (
    ConstantPdaSeedNode,
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    derive_pda,
)
from .program_graph import ProgramGraph, build_program_graph
from .value_sources import value_sources

__all__ = [
    "DERIVE_ATA_TOOL",
    "DERIVE_PDA_TOOL",
    "PREPARE_INSTRUCTION_TOOL",
    "derive_ata_result",
    "derive_pda_result",
    "PrepareInstructionRefusal",
    "AccountOrigin",
    "plan_accounts",
    "prepare_instruction_result",
]

#: How each account in the plan got its address. This is the provenance an agent needs to
#: decide how much to trust the call, and the field an A2A capability card would carry:
#: ``pinned`` is the program's own word, ``derived`` is computed from the graph,
#: ``supplied`` is the caller's claim and is the only one nobody has verified.
AccountOrigin = Literal["pinned", "derived", "supplied"]

#: The associated-token program, recognised only to REPORT that a sibling account is an
#: ATA — never to infer that an unconstrained slot is one.
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
#: The legacy SPL Token program — the default a caller means unless they say Token-2022.
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

PrepareInstructionRefusal = Literal[
    "argument-invalid",
    "program-unknown",
    "instruction-unknown",
    "accounts-unresolved",
    "argument-missing",
    "build-failed",
    "simulation-reverted",
]

#: Resolve a program id to its IDL. Injected, so the whole path is falsifiable offline and
#: so the catalogue this composes with is the caller's choice, not this module's.
IdlFetch = Callable[[str], dict[str, Any]]
#: Build an unsigned transaction from a fully-resolved plan. Injected for the same reason —
#: and because the builder that does this best is usually the catalogue's own.
BuildCall = Callable[..., str]
RpcCall = Callable[[str, str, list[Any]], dict[str, Any]]


def _refuse(
    code: PrepareInstructionRefusal, reason: str, **extra: Any
) -> dict[str, Any]:
    return {"refused": True, "code": code, "reason": reason, **extra}


def plan_accounts(
    graph: ProgramGraph,
    instruction: str,
    values: Mapping[str, Any],
    payer: str | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve every account slot of ``instruction``.

    Returns ``(resolved, origins, missing)``. An account is resolved from, in order: the
    address the IDL PINS (the program's own word, and never something to ask a caller for
    — asking is how a flow ends up parameterising the token program), a value the caller
    SUPPLIED, the PAYER for the ONE plain signer slot nobody filled, or DERIVATION from
    the graph. Anything left is reported with the seeds it still needs, never filled in.

    The payer rule earns its place exactly once per instruction: an instruction's lone
    open signer slot is the actor, the payer signs, and making a caller repeat their own
    address under whatever local name the program chose (`contributor`, `user`,
    `authority`) is a question with one possible answer. It is recorded as `supplied`,
    not `derived` — nobody verified it.

    IT DOES NOT EARN THE SECOND SLOT, and this used to fill every one of them. Measured
    on the captured IDLs: meteora `initialize_position` is signed by [payer, position,
    owner], `initialize_position_by_operator` by [payer, base, operator],
    `rebalance_liquidity` by [owner, rent_payer], pump.fun `create` by [mint, user] — all
    of them got the payer's single key. Nothing downstream catches that: this module
    simulates with `sigVerify: False`, which cannot tell one actor from three, so the
    residual is a clean-simulating transaction with the WRONG ACTOR in it. Several open
    signers is a question with several answers, so it is asked, not answered.

    A signer slot that is a PDA is never the payer's either, whatever the count: a
    program-derived signer is signed for by the program, and the payer's key in that slot
    is wrong AND cascades into every sibling that seeds on it (pump.fun
    `set_mayhem_virtual_params`: `mayhem_token_vault` seeds on the `sol_vault_authority`
    PDA). Those are left to PASS 2, which derives them.
    """
    target = next((ix for ix in graph.instructions if ix.name == instruction), None)
    if target is None:
        return {}, [], []

    resolved: dict[str, str] = {}
    origins: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    by_name = {a.name: a for a in target.accounts}

    # PASS 1 — everything that needs no derivation.
    #
    # THIS PASS EXISTS BECAUSE IDL ORDER IS NOT DEPENDENCY ORDER. jurassic_fi's
    # `contribute` lists `payment_vault` (index 5) BEFORE `token_program` (index 6), and
    # the vault's associated-token recipe seeds on the token program — so a single walk in
    # IDL order tried to derive the vault while its own seed was still unresolved, and
    # reported a missing binding for an account the instruction PINS two slots later.
    for account in target.accounts:
        if account.address:
            resolved[account.name] = account.address
            origins.append({"account": account.name, "origin": "pinned"})
            continue
        supplied = values.get(account.name)
        if isinstance(supplied, str) and supplied:
            resolved[account.name] = supplied
            origins.append({"account": account.name, "origin": "supplied"})
            continue

    # PASS 1b — the payer, and only into the slot where it is the ONE answer.
    #
    # Runs before PASS 2 because derived accounts seed on signer slots (jurassic_fi's
    # `user_position` seeds on `contributor`), so the binding has to exist by then.
    open_signers = tuple(
        a
        for a in target.accounts
        if a.signer and not a.is_pda and a.name not in resolved
    )
    if payer and len(open_signers) == 1:
        only = open_signers[0]
        resolved[only.name] = payer
        origins.append({"account": only.name, "origin": "supplied"})
        open_signers = ()

    # PASS 2 — derive the PDAs in DEPENDENCY order, which the graph already computed.
    for name in target.derivation_order:
        candidate = by_name.get(name)
        if candidate is None or name in resolved:
            continue
        account = candidate
        # THIS INSTRUCTION'S recipe, not the program-wide one keyed by account name.
        # `resolvable` was always judged from the per-instruction AccountRef and the
        # derivation was done with `graph.pdas[name]`; where the two diverge — and
        # `build_program_graph` documents that they do (Orca `whirlpool`, stableswap
        # `token_vault`, and ORE's `miner`, declared on `authority` by `deploy` and on
        # `signer` by five earlier instructions) — that judged one node and derived
        # another. `graph.pdas` remains the fallback for a graph built by some other
        # producer, which carries no per-account recipe.
        node = account.pda or graph.pdas.get(name)
        if node is None or not account.resolvable:
            missing.append(
                {
                    "account": name,
                    "why": "the graph flags this recipe unresolvable",
                    "needs": sorted(
                        {b.seed_name for b in account.derive_from if not b.encoding}
                    ),
                }
            )
            continue
        bindings: dict[str, Any] = {**resolved, **values}
        aliased = seed_aliases(node, bindings)
        bindings.update({seed: value for seed, (_from, value) in aliased.items()})
        try:
            resolved[name] = derive_pda(node, bindings).address
        except Exception as exc:  # noqa: BLE001 - a failure to derive is an ANSWER
            missing.append(
                {
                    "account": name,
                    "why": f"{type(exc).__name__}: {exc}",
                    "needs": sorted(_unbound(node, bindings)),
                }
            )
            continue
        origin: dict[str, Any] = {"account": name, "origin": "derived"}
        if aliased:
            # Never silent. The caller sees which of their names filled which seed.
            origin["aliased_seeds"] = {
                seed: source for seed, (source, _value) in aliased.items()
            }
        origins.append(origin)

    # PASS 3 — whatever is still absent, named with what it would take.
    several = {a.name for a in open_signers} if len(open_signers) > 1 else set()
    for account in target.accounts:
        if account.name in resolved or any(
            m["account"] == account.name for m in missing
        ):
            continue
        missing.append(
            {
                "account": account.name,
                "why": (
                    _why_several_signers(open_signers)
                    if account.name in several
                    else _why_absent(account, target, graph)
                ),
                "signer": account.signer,
            }
        )

    return resolved, origins, missing


def _why_several_signers(open_signers: tuple[Any, ...]) -> str:
    """Why the payer was NOT poured into every open signer slot.

    Names all of them, because the ambiguity is the answer: the caller can see that the
    instruction wants several distinct actors and which ones, instead of being told
    "supply this" about each slot separately.
    """
    named = ", ".join(f"`{a.name}`" for a in open_signers)
    return (
        f"this instruction is signed by {len(open_signers)} distinct actors "
        f"({named}) and the payer is only one of them — supply each by name. The "
        "payer fills a signer slot only when it is the last one open, where it is "
        "the single possible answer. Simulation here runs with `sigVerify` disabled "
        "and cannot tell one signer from several, so guessing would not be caught."
    )


def _why_absent(account: Any, instruction: Any, graph: ProgramGraph) -> str:
    """Why an account could not be filled, said so a caller can act on it.

    For a plain account the IDL does not constrain, "the caller supplies this" is true and
    thin. When a SIBLING account in the same instruction is declared as an associated
    token account, that is a fact worth passing on — the caller can decide whether this
    slot is the same shape. Stated as the sibling's recipe, never as a conclusion about
    this one: inferring an ATA and deriving it would be the guess this module refuses.
    """
    if account.is_pda:
        return "declared as a PDA but no recipe was recovered for it"
    for other in instruction.accounts:
        if other.name == account.name or not other.is_pda:
            continue
        node = graph.pdas.get(other.name)
        if node is not None and node.program_id == ATA_PROGRAM:
            return (
                "not a PDA in this IDL — the caller supplies it. The sibling "
                f"`{other.name}` IS declared as an associated token account, so if this "
                "slot is the same shape for a different owner, call `derive_ata` with "
                "that owner and the mint. DO NOT hand-roll the bump loop: one caller "
                "did, skipped the on-curve check, and got a valid-looking address at "
                "bump 255 where the real one was 254."
            )
    return "not a PDA — the caller must supply this address"


def seed_aliases(
    node: PdaNode, values: Mapping[str, Any]
) -> dict[str, tuple[str, Any]]:
    """Accept the spelling the caller was GIVEN for a seed this recipe spells differently.

    A LIVE AGENT LOST A FULL ROUND TRIP TO THIS. ``find_start`` advertises the input as
    ``launch_id``; the recipe's seed is ``params.launch_id``, because Anchor writes an
    argument-struct field as a dotted path. The agent supplied exactly what it was told to
    supply and was refused for a missing binding.

    This is a SPELLING accommodation and nothing else. It binds the dotted seed
    ``a.b`` from a caller value spelled ``b``, and the plain seed ``b`` from a caller
    value spelled ``a.b`` — and only when the short name is unambiguous across this
    recipe's seeds. Two seeds ending in the same segment alias nothing: substituting a
    value under an ambiguous name is a guess, and a wrong seed derives a real, correctly
    formatted, wrong address.
    """
    names = {
        seed.name
        for seed in node.seeds
        if isinstance(seed, (VariablePdaSeedNode, ResolverPdaSeedNode))
    }
    tails: dict[str, list[str]] = {}
    for name in names:
        if "." in name:
            tails.setdefault(name.rsplit(".", 1)[1], []).append(name)

    aliased: dict[str, tuple[str, Any]] = {}
    for tail, owners in tails.items():
        if len(owners) != 1:
            continue  # ambiguous — two dotted seeds share this last segment
        (dotted,) = owners
        if dotted not in values and tail in values:
            aliased[dotted] = (tail, values[tail])
    # and the mirror: the caller spelled it `params.launch_id` where the seed is plain
    for key, value in values.items():
        if "." not in str(key):
            continue
        tail = str(key).rsplit(".", 1)[1]
        if tail in names and tail not in values:
            aliased[tail] = (str(key), value)
    return aliased


def _unbound(node: PdaNode, bindings: Mapping[str, Any]) -> set[str]:
    """The seed names a recipe still wants — what a caller must go and find."""
    return {
        seed.name
        for seed in node.seeds
        if isinstance(seed, (VariablePdaSeedNode, ResolverPdaSeedNode))
        and seed.name not in bindings
    }


def prepare_instruction_result(
    arguments: Mapping[str, Any],
    *,
    idl_fetch: IdlFetch,
    build_call: BuildCall,
    rpc_call: RpcCall | None = None,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Plan, build and simulate one instruction. Never raises for an answer.

    An expected outcome — an unknown instruction, an account nobody can derive, a
    simulation that reverts — comes back as a structured refusal carrying no transaction,
    because each is something the caller must handle rather than retry.
    """
    args = arguments or {}
    program_id = str(args.get("program_id") or "").strip()
    instruction = str(args.get("instruction") or "").strip()
    values: Mapping[str, Any] = args.get("values") or {}
    payer = str(args.get("payer") or "").strip()

    if not program_id:
        return _refuse("program-unknown", "no program_id was given")

    try:
        idl = idl_fetch(program_id)
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            "program-unknown",
            f"the catalogue could not resolve {program_id}: {type(exc).__name__}",
        )

    graph = build_program_graph(idl=idl, program_id=program_id)
    names = [ix.name for ix in graph.instructions]
    if instruction not in names:
        return _refuse(
            "instruction-unknown",
            f"{instruction!r} is not an instruction of this program",
            available=names,
        )

    target = next(ix for ix in graph.instructions if ix.name == instruction)
    declared_args = [name for name, _ in target.args]
    absent = [name for name in declared_args if name not in values]
    if absent:
        return _refuse(
            "argument-missing",
            f"{instruction} declares {len(declared_args)} argument(s) and "
            f"{len(absent)} were not supplied",
            missing_arguments=[
                {"name": name, "type": ty} for name, ty in target.args if name in absent
            ],
        )

    resolved, origins, missing = plan_accounts(graph, instruction, values, payer)
    if missing:
        # Name where the missing values can be READ from. Until now this refusal said
        # "the caller supplies it" and stopped, while `read_accounts` — on this same
        # surface — could already return them for 66.9% of PDA accounts that need values.
        # Two of our own tools, one holding what the other needs.
        #
        # A source, never a value: nothing is fetched and no instance is chosen here,
        # because a resolved address decides who gets paid.
        wanted: list[str] = []
        for entry in missing:
            for value in entry.get("needs", ()) if isinstance(entry, dict) else ():
                if value not in wanted:
                    wanted.append(str(value))
        sources = (
            value_sources(idl, program_id, instruction=instruction, needed=wanted)
            if wanted
            else {}
        )
        return _refuse(
            "accounts-unresolved",
            f"{len(missing)} account(s) of {instruction} could not be resolved; a "
            "transaction is not built from a partial plan",
            missing_accounts=missing,
            resolved_accounts=resolved,
            value_sources=sources,
        )

    # WE FETCH THE BLOCKHASH, so we can state when these bytes STOP BEING LANDABLE.
    #
    # Leaving it to the builder costs the caller the one number they need most. A hosted
    # signer that spends the window loading its own tools between prepare and sign gets a
    # bare `BlockhashNotFound` and nothing to tell it the cause was TIME. `prepare_purchase`
    # learned this the hard way and reports a budget; there is no reason the other 4,000+
    # programs in a catalog should inherit the lesson separately.
    blockhash: str | None = None
    last_valid: int | None = None
    current_height: int | None = None
    if rpc_call and rpc_url:
        try:
            latest = (
                rpc_call(rpc_url, "getLatestBlockhash", [{"commitment": "finalized"}])
                .get("result", {})
                .get("value", {})
            )
            blockhash = latest.get("blockhash")
            last_valid = latest.get("lastValidBlockHeight")
            current_height = (rpc_call(rpc_url, "getBlockHeight", []) or {}).get(
                "result"
            )
        except Exception:  # noqa: BLE001 - an absent budget is honest; a wrong one is not
            blockhash = last_valid = current_height = None

    try:
        build_kwargs: dict[str, Any] = {
            "program_id": program_id,
            "instruction": instruction,
            "accounts": resolved,
            "args": {name: values[name] for name in declared_args},
            "payer": payer,
        }
        if blockhash:
            build_kwargs["blockhash"] = blockhash
        try:
            transaction = build_call(**build_kwargs)
        except TypeError:
            # a builder that does not accept a blockhash fetches its own; the budget is
            # then unknown, and saying so beats reporting one that is not this tx's
            build_kwargs.pop("blockhash", None)
            blockhash = last_valid = current_height = None
            transaction = build_call(**build_kwargs)
    except Exception as exc:  # noqa: BLE001
        return _refuse(
            "build-failed", f"the builder refused: {type(exc).__name__}: {exc}"
        )

    # THE BINDING IS WHAT MAKES `verify_signed_transaction` REACHABLE HERE.
    #
    # Without it that tool only works for one storefront, and every other program in the
    # catalog loses the ability to prove — AFTER signing, BEFORE broadcast — that the bytes
    # coming back are the bytes that were checked. That is the strongest safety property on
    # this surface, and it should not stop at the edge of one program.
    binding: str | None = None
    strength: str | None = None
    try:
        from .txbind import BindingStrength, message_binding

        # `exact` because these bytes carry OUR blockhash and are the ones that will be
        # signed. Structural is right when a simulation replaced the blockhash; here it
        # would bind less than we can honestly bind.
        chosen: BindingStrength = "exact" if blockhash else "structural"
        binding = message_binding(transaction, encoding="base64", strength=chosen)
        strength = chosen
    except Exception:  # noqa: BLE001 - a binding we cannot compute is absent, not fatal
        binding = None
        strength = None

    result: dict[str, Any] = {
        "refused": False,
        "signed": False,
        "instruction": instruction,
        "program_id": program_id,
        "accounts": resolved,
        "account_origins": origins,
        "derivation_order": list(target.derivation_order),
        "transaction_base64": transaction,
        "binding": binding,
        "binding_strength": strength,
        "next_step": (
            "sign these exact bytes, then call `verify_signed_transaction` with this "
            "`binding` BEFORE broadcasting — it catches a signer that returned different "
            "bytes than the ones checked here."
            if binding
            else "no binding could be computed for these bytes; "
            "`verify_signed_transaction` cannot check them."
        ),
    }
    if last_valid is not None:
        remaining = (
            None if current_height is None else max(0, last_valid - current_height)
        )
        result["expires"] = {
            "blockhash": blockhash,
            "last_valid_block_height": last_valid,
            "current_block_height": current_height,
            "blocks_remaining": remaining,
            "seconds_remaining_estimate": (
                None if remaining is None else round(remaining * 0.4)
            ),
            "note": (
                "these bytes stop being landable once the chain passes "
                "`last_valid_block_height`. Re-calling this is free. Load your signer's "
                "tools BEFORE calling it: a cold client can spend the whole budget "
                "discovering how to sign."
            ),
        }

    if rpc_call and rpc_url:
        simulation = rpc_call(
            rpc_url,
            "simulateTransaction",
            [
                transaction,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                },
            ],
        )
        value = (simulation.get("result") or {}).get("value") or {}
        if value.get("err") is not None:
            # Named from the FULL log, then truncated for display. The origin of a CPI'd
            # error is the FIRST failure line, which a deep call stack can push outside
            # the last twelve — naming off the truncated view would credit whichever
            # program happened to survive the cut.
            named = name_program_error(
                value.get("err"),
                logs=value.get("logs") or (),
                idl=idl,
                program_id=program_id,
            )
            extra: dict[str, Any] = {
                "error": value.get("err"),
                "logs": (value.get("logs") or [])[-12:],
                "accounts": resolved,
            }
            if named is not None:
                # A DISTINCT field: the raw `error` is the runtime's, untouched, and this
                # is what the program's own table says about it — never merged, so a
                # reader can always tell which is which.
                detail: dict[str, Any] = {
                    "code": named.code,
                    "name": named.name,
                    "message": named.message,
                    "program": named.program,
                    "source": named.source,
                    "unnamed_because": named.unnamed_because,
                }
                # Gecko's advice about the error rides in ITS OWN nested field, labelled
                # as ours. Only attached when the error was actually attributed to a
                # program — advice about an error we could not place is a guess.
                advice = remediation_for(named.program, named.code)
                if advice is not None:
                    detail["remediation"] = {
                        "guidance": advice.guidance,
                        "established_by": advice.established_by,
                        "source": advice.source,
                    }
                extra["program_error"] = detail
            reason = "the program rejected this call; the bytes are not handed over"
            if named is not None and named.named:
                reason = (
                    f"the program rejected this call with {named.describe()}; "
                    "the bytes are not handed over"
                )
            return _refuse("simulation-reverted", reason, **extra)
        result["simulation"] = {
            "err": None,
            "compute_units": value.get("unitsConsumed"),
        }

    return result


#: The agent-facing tool. The description states the ORDER and the two things an agent
#: gets wrong unaided: that values it does not hold must come from a chain read, and that
#: an instruction's SECOND argument is as load-bearing as its first.
PREPARE_INSTRUCTION_TOOL = {
    "name": "prepare_instruction",
    "annotations": tool_annotations(
        read_only=True, open_world=True, title="Prepare an unsigned instruction"
    ),
    "description": (
        "Build ANY instruction of ANY program in the catalog, with every PDA derived "
        "for you, and get back UNSIGNED bytes plus a mainnet simulation. Nothing here "
        "signs or broadcasts.\n"
        "\n"
        "Pass `values` with everything you already hold: the accounts you own or chose, "
        "and EVERY declared argument. What you do not pass, this derives — and what it "
        "cannot derive it names, with the seeds still missing, instead of guessing. A "
        "guessed seed produces a well-formed address for an account that does not exist, "
        "which nothing downstream catches.\n"
        "\n"
        "TWO THINGS AGENTS GET WRONG HERE. (1) Some seeds are fields of the account being "
        "derived — read them off-chain first (`read_accounts` does it and proves each "
        "answer by re-derivation) and pass them as values; the refusal will name exactly "
        "which. (2) An instruction's later arguments are not optional detail: a "
        "minimum-amount or slippage argument is how you say what you will NOT accept, "
        "and omitting it is refused rather than defaulted.\n"
        "\n"
        "Each account comes back saying how it got its address — `pinned` (the program's "
        "own word), `derived` (computed here), `supplied` (your claim, and the only one "
        "nobody verified)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "program_id": {
                "type": "string",
                "description": "the Solana program address (base58)",
            },
            "instruction": {
                "type": "string",
                "description": "the instruction name, exactly as the IDL spells it",
            },
            "payer": {
                "type": "string",
                "description": "base58 address that pays the fee and signs",
            },
            "values": {
                "type": "object",
                "description": (
                    "account name -> base58 address, and argument name -> value. Seed "
                    "values read off-chain go here too. Either spelling of a dotted seed "
                    "works — `launch_id` and `params.launch_id` both bind the seed "
                    "`params.launch_id`, so the name `find_start` gave you is accepted "
                    "as-is; the response says which of your names filled which seed."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["program_id", "instruction", "payer"],
        "additionalProperties": False,
    },
}

# ---------------------------------------------------------------------------
# derivation as a PRIMITIVE
# ---------------------------------------------------------------------------

DERIVE_ATA_TOOL = {
    "name": "derive_ata",
    "annotations": tool_annotations(
        read_only=True, title="Derive an associated token account"
    ),
    "description": (
        "The associated token account for an owner + mint. Use this instead of computing "
        "it yourself.\n"
        "\n"
        "WHY THIS TOOL EXISTS. An agent following a correct refusal hand-rolled this "
        "derivation, skipped the ed25519 on-curve check, and produced a well-formed WRONG "
        "address at bump 255 — the real one is 254. Refusing to guess is right; refusing "
        "in a way that pushes the guess outside where it can be checked is not. The loop "
        "is subtle, the failure is silent, and nobody should write it twice."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "owner": {
                "type": "string",
                "description": "base58 wallet that owns the tokens",
            },
            "mint": {"type": "string", "description": "base58 token mint"},
            "token_program": {
                "type": "string",
                "description": (
                    "base58 token program — REQUIRED, and it is a property OF THE MINT, "
                    "not of the owner. It is the SECOND SEED, so classic SPL Token and "
                    "Token-2022 derive DIFFERENT addresses for the same owner and mint. "
                    "Read the mint account's `owner` field; that IS the token program. "
                    "Never infer it from the mint address, its decimals, or a label — "
                    "the wrong one is not rejected, it is a valid, off-curve address for "
                    "an account that was never initialized."
                ),
            },
        },
        "required": ["owner", "mint", "token_program"],
        "additionalProperties": False,
    },
}

DERIVE_PDA_TOOL = {
    "name": "derive_pda",
    "annotations": tool_annotations(read_only=True, title="Derive a program address"),
    "description": (
        "Derive a program address from explicit seeds, with the bump found the way the "
        "runtime finds it — descending from 255 until the result is OFF the ed25519 "
        "curve. A hand-rolled loop that skips the curve check returns a plausible address "
        "for an account that cannot exist."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "program_id": {"type": "string"},
            "seeds": {
                "type": "array",
                "description": (
                    "in order. Each entry is {utf8}, {pubkey}, or {u64|u32|u16|u8} — the "
                    "integer width MATTERS: the same value at u8 and u64 derives two "
                    "different valid addresses."
                ),
                "items": {"type": "object"},
            },
        },
        "required": ["program_id", "seeds"],
        "additionalProperties": False,
    },
}


def derive_ata_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The ATA for owner+mint, derived — never guessed."""
    args = arguments or {}
    owner = str(args.get("owner") or "").strip()
    mint = str(args.get("mint") or "").strip()
    token_program = str(args.get("token_program") or "").strip()
    if not owner or not mint:
        return _refuse("argument-missing", "derive_ata needs an `owner` and a `mint`")
    if not token_program:
        # This defaulted to the legacy SPL Token program, which reproduced THIS TOOL'S
        # OWN documented failure mode one field over: a well-formed WRONG address
        # returned with `refused: false`, for every Token-2022 mint whose caller left
        # the field out. The token program is a property OF THE MINT — it cannot be
        # inferred from the mint address, its decimals, or its label, so absent is a
        # refusal, not an assumption.
        return _refuse(
            "argument-missing",
            "derive_ata needs a `token_program`: it is the SECOND SEED, so the classic "
            "and Token-2022 programs derive DIFFERENT addresses for the same owner and "
            "mint, and the wrong one is not rejected — it is a valid, off-curve address "
            "for an account that was never initialized. Read the mint account's `owner` "
            "field (that IS the token program) and pass it; never infer it from the "
            f"mint address, its decimals, or a label. Classic is {TOKEN_PROGRAM}.",
        )
    try:
        derived = derive_pda(
            PdaNode(
                "ata",
                (
                    VariablePdaSeedNode("owner", source="account", encoding="pubkey"),
                    VariablePdaSeedNode(
                        "token_program", source="account", encoding="pubkey"
                    ),
                    VariablePdaSeedNode("mint", source="account", encoding="pubkey"),
                ),
                ATA_PROGRAM,
            ),
            {"owner": owner, "token_program": token_program, "mint": mint},
        )
    except Exception as exc:  # noqa: BLE001
        return _refuse("argument-invalid", f"{type(exc).__name__}: {exc}")
    return {
        "refused": False,
        "address": derived.address,
        "bump": derived.bump,
        "owner": owner,
        "mint": mint,
        "token_program": token_program,
        "note": (
            "the bump is the first value from 255 downward that lands OFF the ed25519 "
            "curve; a loop that omits that check can return a different, unusable address."
        ),
    }


def derive_pda_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """One program address from explicit, ordered seeds."""
    args = arguments or {}
    program_id = str(args.get("program_id") or "").strip()
    raw_seeds = args.get("seeds")
    if not program_id or not isinstance(raw_seeds, list) or not raw_seeds:
        return _refuse(
            "argument-missing", "derive_pda needs a `program_id` and `seeds`"
        )

    seeds: list[Any] = []
    bindings: dict[str, Any] = {}
    for index, seed in enumerate(raw_seeds):
        if not isinstance(seed, Mapping) or len(seed) != 1:
            return _refuse(
                "argument-invalid",
                f"seed {index} must be exactly one of "
                "{utf8|pubkey|u64|u32|u16|u8}: <value>",
            )
        kind, value = next(iter(seed.items()))
        name = f"s{index}"
        if kind == "utf8":
            seeds.append(
                ConstantPdaSeedNode(value=str(value).encode(), encoding="utf8")
            )
            continue
        if kind == "pubkey":
            seeds.append(VariablePdaSeedNode(name, source="account", encoding="pubkey"))
            bindings[name] = str(value)
            continue
        widths = {"u64": 8, "u32": 4, "u16": 2, "u8": 1}
        if kind in widths:
            seeds.append(
                VariablePdaSeedNode(
                    name, source="argument", encoding="le", width=widths[kind]
                )
            )
            bindings[name] = int(value)
            continue
        return _refuse("argument-invalid", f"seed {index}: unknown kind {kind!r}")

    try:
        derived = derive_pda(PdaNode("pda", tuple(seeds), program_id), bindings)
    except Exception as exc:  # noqa: BLE001
        return _refuse("argument-invalid", f"{type(exc).__name__}: {exc}")
    return {
        "refused": False,
        "address": derived.address,
        "bump": derived.bump,
        "program_id": program_id,
    }
