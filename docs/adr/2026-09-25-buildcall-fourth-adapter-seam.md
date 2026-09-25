# ADR — `BuildCall` is the fourth adapter seam, and the build default moves in-engine

**Status:** Accepted · 2026-09-25
**Deciders:** founder, on a `staff-engineer` ruling
**Supersedes:** nothing. **Superseded by:** nothing.

## Context

A purchase failed on mainnet:

```
SimulateError: build POST to https://api.orquestra.dev/api/<project>/instructions/
make_purchase/build failed: HTTP 500 Internal Server Error
```

The hosted builder answered 500 on five of six identical requests. Its response body
said `Failed to fetch recent blockhash: RPC request failed: HTTP 429` — its own
upstream RPC, rate-limiting **a blockhash Gecko discards**. We re-stamp a fresh one at
the layout offset before signing. Nobody saw that body, because `simulate.py` kept the
status code and threw the rest away under a comment claiming redaction posture.

Two facts made this decidable rather than a matter of taste.

**We already owned every input.** For `make_purchase` the hosted call contributed
`discriminator(8) ‖ borsh(store) ‖ borsh(product) ‖ u8(table)` and nothing else. The
accounts are derived offline (`prepare_purchase.py:419-446`, zero chain reads, no
hosted derive endpoint asked). The discriminator comes from
`artifact.instruction_encoding`. The message compiler is `landing.assemble_unsigned_tx`.
We were paying a network round trip, inside a ~60-second blockhash window, to
concatenate bytes we already describe.

**The seam had already drifted.** `BuildCall` was declared twice, incompatibly:

```
gecko/simulate.py:109            BuildCall = Callable[[Mapping[str, Any]], "BuiltTx"]
gecko/prepare_instruction.py:82  BuildCall = Callable[..., str]
```

One name, two return types, one of them a typed object carrying its encoding and the
other a bare string. That is the project's "single source of truth for shared types,
never redeclare" rule broken in the open. Invariant #2 in `CLAUDE.md` says a fourth
adapter seam needs an explicit ruling; `BuildCall` had been operating as one — injected
at every call site, defaulted to a single vendor, and the thing that makes the whole
path falsifiable offline — without ever getting one. The invariant exists to catch
exactly this, and it did not, because nobody counted.

## Decision

**One.** `BuildCall` is admitted as the **fourth** adapter seam, beside
`Session.auth_headers()` (access), `DenseIndex` (retrieval) and `Enricher` (authored
prose). A **fifth** needs its own record. The count in the invariant is load-bearing;
that is why the sentence names a number.

**Two.** One canonical declaration, in `gecko/simulate.py`. `prepare_instruction.py`
imports it. Its `str`-shaped callers adapt.

**Three.** The seam's **default** moves from the hosted endpoint to
`gecko/instruction_build.py`, which encodes an instruction from the IDL's own shape as
data. The hosted `/build` remains the **fallback** for instructions we cannot encode.
The injection point does not move: an injected `build_call` still replaces the default
everywhere, which is what keeps the path falsifiable offline.

**Four.** The encoder **refuses by name** rather than guessing. Every `{"defined": …}`
type, `f32`/`f64`, any unknown scalar, and any value that does not fit its declared
type. There is no default branch in `_encode_value`. A guessed byte in an instruction
is a wrong transaction that looks right.

**Five.** `simulate.py` keeps the builder's error body — bounded, sanitised through the
existing helper, dropped whole if it trips the secret check. The old comment was wrong
for this call site: our own docstring says the builder is a user-configured HTTP target,
not ingested spec content, so its error was never untrusted text to scrub.

## What this forbids

- Adding a fifth injected seam without a record here.
- Re-declaring `BuildCall` anywhere.
- Adding a program-specific branch to `gecko/instruction_build.py`. A second program's
  local build must touch its config and nothing in the engine;
  `test_a_second_program_builds_with_no_engine_change` and
  `test_the_engine_module_names_no_program` enforce it. If the engine needs the branch,
  the abstraction is wrong and that is the finding, not the workaround.
- Encoding an arg type by inference. Refuse and name it.

## Alternatives, and what they cost

**(a) Keep the hosted builder as default, local as fallback.** Cheapest to ship, and it
keeps today's failure mode as the normal case. Every purchase still spends a round trip
inside the blockhash window on a dependency measured at ~83% failure in one sample, and
the local path stays cold and rots. A fallback exercised only during an outage is a
fallback that fails during an outage — Pattern B, inverted.

**(c) Drop the partner from the live path entirely, keep them as a catalog input.** The
right end state and the wrong next step. It needs a generic encoder covering five other
programs' arg types (the `OptionBool` class is a known open gap), an on-chain or
vendored IDL source to replace `/api/idl/<project>`, and a decision about `find_start`
without their 4,499-program catalog. Weeks, and it ends a partnership by attrition
rather than by a conversation.

## Reversibility

**One-way**, and not for the reason it looks. The file layout is two-way. The **default**
is not: flipping who builds the bytes changes what the public MCP surface, `docs/proofs.md`
and `providers/orquestra.py` claim about the partnership. Unflipping later costs a story,
not a revert.

## Consequences

**The compose sentence shrinks, and we say so out loud.** `providers/orquestra.py` says
*"Gecko is the metadata/control plane; Orquestra runs the tx"* and `landing.py` says
*"Orquestra builds the program instruction (the IDL-hard part)."* For `make_purchase`
those are now false. Four of nine things we use the partner for remain, including the
two with real scale, so the partnership survives. The clean sentence does not.

It had been drifting before this decision, which is the uncomfortable part: we already
discarded their blockhash, refused their plan before asking for it, re-normalised their
signature slots and assembled the landing bundle ourselves. "He runs the tx" had meant
"he concatenates the instruction data" for some time.

**The partner hears it from us, with the log, before the docstrings change.** A design
partner whose builder 500s five times in six on a money flow cannot be on the critical
path, and saying that is a better partnership act than silently routing around it. The
compose story survives a conversation. It does not survive us deleting the call and
leaving the docstring up.

**One check got stronger on the way.** The old code decided "did the builder honour our
blockhash" by catching a `TypeError` on the builder's Python **signature**. That could
never see a builder which *accepts* `blockhash` and stamps its own anyway — which is
what the hosted one did, roughly 4,500 blocks stale. It now reads the blockhash back out
of the **built bytes**. We were checking a signature where we should have been checking
the artifact, which is this project's own thesis turned on us.

## Evidence

Bytes pinned against the chain, not against ourselves: data, program id, account order
and writable/signer flags equal the instruction that landed in mainnet signature
`4WmhBh4y…` (`docs/mainnet-ledger.jsonl`, 2026-09-20), decoded with solders from
`getTransaction`. The message key table is deliberately not compared — web3.js and
solana-sdk order it differently and the program never sees it.

Shipped in #575. `tests/test_instruction_build.py` (46), `tests/test_let_me_buy_local_build.py`
(14, carried over byte-identical from the spike and passing unchanged against the generic
encoder), `tests/test_simulate.py` (37), `tests/test_prepare_instruction.py` (19).
