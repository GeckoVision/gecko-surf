"""The bridge to Orquestra's real engine — does HIS compiler accept what we project?

Every test here runs code we did not write. `gecko.project.fdl` renders a
`ProgramGraph` into his Flow Definition Language; the only authority on whether
that document is valid, and on what it derives, is the engine that executes it.
So these tests shell out to `bun` and import his compiler, his schema and his
interpreter from a local checkout.

Three lanes, and the markers keep them apart:

  (unmarked) the typed refusals — bun missing, no checkout. Pure Python.
  `bun`      compile + pure-node runs. Offline: `resolve.pda@1` is `effect: "pure"`,
             so his interpreter derives addresses with no network at all.
  `rpc`      one end-to-end run against a real RPC. Opt-in via GECKO_BRIDGE_RPC_URL,
             and it reads its storefront identities from ~/.gecko/catalog-ci.json —
             a public test file has no business naming a wallet or a store.

Nothing here signs or broadcasts. The `rpc` lane fetches a blockhash and simulates.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from gecko.program_graph import build_program_graph
from gecko.project.fdl import to_fdl
from scripts.orquestra_bridge import (
    BunMissingError,
    OrquestraCheckoutMissingError,
    compile_fdl,
    let_me_buy_project,
    orquestra_root,
    run_fdl,
)
from scripts import orquestra_bridge

FIXTURES = Path(__file__).parent / "fixtures"
PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: Chain facts. The receipts PDA of the store this repo's other tests already pin,
#: and the compute the program charges for one make_purchase — both read off mainnet.
JONASBAR_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"
MAKE_PURCHASE_CU = 36399


def _engine_available() -> bool:
    if shutil.which("bun") is None:
        return False
    try:
        orquestra_root()
    except OrquestraCheckoutMissingError:
        return False
    return True


needs_engine = pytest.mark.skipif(
    not _engine_available(),
    reason="needs `bun` on PATH and an Orquestra checkout (GECKO_ORQUESTRA_ROOT)",
)


def make_purchase_doc() -> dict[str, Any]:
    idl = json.loads((FIXTURES / "let_me_buy_idl.json").read_text())
    return to_fdl(
        build_program_graph(idl=idl),
        "make_purchase",
        slug="let-me-buy-make-purchase",
        intent="buy a product from a storefront",
    )


def pda_only_doc(seeds: list[Any]) -> dict[str, Any]:
    """The smallest document his engine will run: one pure `resolve.pda@1` node.
    No RPC, no IDL registry — just his seed encoder and his derivation."""
    return {
        "fdl": "1.0",
        "meta": {
            "slug": "seed-probe",
            "name": "Seed probe",
            "intent": "derive one PDA",
        },
        "inputs": {"store_name": {"type": "string"}},
        "outputs": {"address": {"type": "string"}},
        "nodes": [
            {
                "id": "pda_receipts",
                "type": "resolve.pda@1",
                "in": {"program": PROGRAM_ID, "seeds": seeds},
            }
        ],
    }


# ---------------------------------------------------------------------------
# the typed refusals — no bun, no checkout, no network
# ---------------------------------------------------------------------------


def fake_checkout(root: Path) -> Path:
    """The smallest tree `orquestra_root()` accepts — it probes for `compiler.ts`.

    A test about the BUN refusal must not also depend on a real sibling checkout
    existing. That dependency is why this file first went red in CI and green on a
    laptop: `_bridge()` calls `orquestra_root()` BEFORE `_bun()`, so on a machine
    without `../orquestra` the checkout error fires first and the bun assertion is
    never reached. Passing for an ambient reason is not passing.
    """
    engine = root / "packages/worker/src/flow-engine"
    engine.mkdir(parents=True, exist_ok=True)
    (engine / "compiler.ts").write_text("// stand-in; this test never executes it\n")
    return root


def test_missing_bun_names_what_to_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GECKO_ORQUESTRA_ROOT", str(fake_checkout(tmp_path)))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(BunMissingError) as excinfo:
        compile_fdl(pda_only_doc(["receipts"]))
    assert "bun.sh" in str(excinfo.value)


def test_the_checkout_is_probed_before_bun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both refusals are reachable, and this pins WHICH one wins when both apply.

    Without this, the test above could keep passing while silently exercising the
    checkout guard on one machine and the bun guard on another — the two failures
    read alike to a reader and not at all alike to a maintainer.
    """
    monkeypatch.setenv("GECKO_ORQUESTRA_ROOT", str(tmp_path / "absent"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(OrquestraCheckoutMissingError):
        compile_fdl(pda_only_doc(["receipts"]))


def test_missing_checkout_names_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GECKO_ORQUESTRA_ROOT", str(tmp_path))
    with pytest.raises(OrquestraCheckoutMissingError) as excinfo:
        compile_fdl(pda_only_doc(["receipts"]))
    message = str(excinfo.value)
    assert str(tmp_path) in message
    assert "bun install" in message


def test_the_default_registry_row_is_our_own_fixture() -> None:
    project = let_me_buy_project()
    assert project.program_id == PROGRAM_ID
    assert project.idl["address"] == PROGRAM_ID
    assert project.project_id  # the id the committed provider config carries


# ---------------------------------------------------------------------------
# his compiler — a good document, and bad ones
# ---------------------------------------------------------------------------


@pytest.mark.bun
@needs_engine
def test_his_compiler_accepts_our_projected_make_purchase() -> None:
    outcome = compile_fdl(make_purchase_doc())

    assert outcome.ok, outcome.messages
    plan = outcome.plan
    assert plan is not None
    # His Kahn batching, not ours: every PDA resolves before the instruction that
    # consumes it, and the transaction composes last.
    assert plan.node_ids == (
        ("pda_receipts", "pda_recipient_token_account", "pda_sender_token_account"),
        ("ix_make_purchase",),
        ("tx",),
    )
    assert len(plan.hash) == 64
    # The document declares no external.http@1 node, and his compiler keeps that policy.
    assert plan.budgets.external_calls == 0


@pytest.mark.bun
@needs_engine
def test_his_compiler_rejects_the_node_types_that_do_not_exist() -> None:
    """`map.over@1` and `flow.call@1` are in his design doc and NOT in his engine.
    A projector that emits either produces a document that lints in a reader's head
    and dies at compile — so the refusal is asserted in his words, not ours."""
    doc = pda_only_doc(["receipts"])
    doc["nodes"].append({"id": "fanout", "type": "map.over@1", "in": {"over": []}})

    outcome = compile_fdl(doc)

    assert not outcome.ok
    assert 'unknown node type "map.over@1"' in outcome.messages
    assert [error.node_id for error in outcome.errors] == ["fanout"]


@pytest.mark.bun
@needs_engine
def test_his_compiler_catches_a_seed_bound_to_an_input_that_was_never_declared() -> (
    None
):
    """The failure mode a projector hits first: a seed references `$inputs.<name>`
    for an arg the document forgot to declare. It is structurally valid JSON and
    still unrunnable."""
    doc = pda_only_doc(["receipts", {"kind": "string", "value": "$inputs.shop"}])

    outcome = compile_fdl(doc)

    assert not outcome.ok
    assert 'unresolvable input reference "$inputs.shop"' in outcome.messages


@pytest.mark.bun
@needs_engine
def test_his_schema_rejects_a_slug_that_is_not_kebab_case() -> None:
    doc = pda_only_doc(["receipts"])
    doc["meta"]["slug"] = "Seed_Probe"

    outcome = compile_fdl(doc)

    assert not outcome.ok
    assert "meta.slug must be kebab-case" in outcome.messages
    assert [error.path for error in outcome.errors] == ["meta.slug"]


# ---------------------------------------------------------------------------
# his interpreter — offline, because resolve.pda@1 is pure
# ---------------------------------------------------------------------------


@pytest.mark.bun
@needs_engine
def test_his_interpreter_derives_the_address_mainnet_confirmed() -> None:
    """The translation, checked by the executor rather than by our own deriver:
    his `encodeSeedEntry` + `findProgramAddressSync` on the seeds we project must
    land on the account the program actually owns."""
    doc = pda_only_doc(["receipts", {"kind": "string", "value": "$inputs.store_name"}])

    outcome = run_fdl(
        doc, {"store_name": "jonasbar"}, rpc_url="http://127.0.0.1:1/unused"
    )

    assert outcome.ok, outcome.error
    assert outcome.node_outputs["pda_receipts"]["address"] == JONASBAR_RECEIPTS
    # A pure stratum: his engine never reached the (deliberately dead) RPC URL.
    assert (outcome.rpc_calls, outcome.external_calls) == (0, 0)


@pytest.mark.bun
@needs_engine
def test_an_idl_seed_kind_is_refused_by_his_encoder() -> None:
    """THE production bug this projector exists to prevent. An Anchor IDL spells its
    seed kinds `const`/`arg`/`account`; `resolve.pda@1` accepts none of them. Passing
    one through is not a lint failure — it compiles, and dies mid-run."""
    doc = pda_only_doc(["receipts", {"kind": "account", "value": "$inputs.store_name"}])

    outcome = run_fdl(
        doc, {"store_name": "jonasbar"}, rpc_url="http://127.0.0.1:1/unused"
    )

    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error.message == 'resolve.pda@1: unknown seed kind "account"'
    assert outcome.error.node_id == "pda_receipts"


@pytest.mark.bun
@needs_engine
def test_the_fake_context_refuses_a_project_it_was_not_given() -> None:
    """A fake that answered `null` here would be indistinguishable from a real
    registry miss, and one that answered the IDL for any id would let a flow name
    the wrong project and pass. It names the key it was asked for instead.

    The surface that refuses moved upstream on 2026-08-17: `fetchProjectIdl` now
    resolves visibility against D1 BEFORE any cache read (checking it only on the
    D1 fallback served a cached private IDL to anyone), so an unknown project id
    is refused at the `projects` row and never reaches the KV registry.
    """
    outcome = run_fdl(
        make_purchase_doc(),
        {
            "projectId": "not-a-registered-project",
            "signer": PROGRAM_ID,
            "authority": PROGRAM_ID,
            "mint": USDC,
            "store_name": "jonasbar",
            "product_name": "Water",
            "table_number": 9,
        },
        rpc_url="http://127.0.0.1:1/unused",
    )

    assert not outcome.ok
    assert outcome.error is not None
    assert (
        'FROM projects WHERE id = "not-a-registered-project"' in outcome.error.message
    )
    assert outcome.error.node_id == "ix_make_purchase"
    # The pure PDA stratum still ran, so the failure is provably the registry read.
    assert outcome.node_outputs["pda_receipts"]["address"] == JONASBAR_RECEIPTS


@pytest.mark.bun
@needs_engine
def test_the_fake_context_has_no_d1_and_says_so() -> None:
    """`resolve.constant@1` reads a `protocol_descriptors` row this bridge has no
    table for. The fake could return `undefined` and let the node hand back an empty
    constant; it throws instead, so "the flow needs a database" can never look like
    "the constant is empty"."""
    doc = {
        "fdl": "1.0",
        "meta": {"slug": "constant-probe", "name": "Constant", "intent": "read one"},
        "inputs": {},
        "outputs": {"value": {"type": "json"}},
        "nodes": [
            {
                "id": "fee",
                "type": "resolve.constant@1",
                "in": {"protocol": "let_me_buy", "key": "fee_bps"},
            }
        ],
    }

    outcome = run_fdl(doc, {}, rpc_url="http://127.0.0.1:1/unused")

    assert not outcome.ok
    assert outcome.error is not None
    # The fake now serves one D1 statement (the project-visibility read), so the
    # tripwire quotes the statement it refused rather than just naming the binding.
    assert outcome.error.message.startswith("ctx.db.prepare(")
    assert "protocol_descriptors" in outcome.error.message
    assert outcome.error.node_id == "fee"


def test_a_document_that_does_not_compile_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_fdl` returns his compile errors instead of raising — "your document is
    invalid" is an answer about the document, not a bridge failure."""

    def fake_invoke(entry: str, payload: Any, **kwargs: Any) -> dict[str, Any]:
        assert entry == "run.ts"
        return {
            "compile": {
                "ok": False,
                "errors": [{"message": "nodes must contain at least one node"}],
            }
        }

    monkeypatch.setattr(orquestra_bridge, "_invoke", fake_invoke)
    outcome = run_fdl({"fdl": "1.0"}, {}, rpc_url="http://127.0.0.1:1/unused")

    assert not outcome.ok
    assert [error.message for error in outcome.compile_errors] == [
        "nodes must contain at least one node"
    ]
    assert outcome.error is None


# ---------------------------------------------------------------------------
# the live lane — opt-in, reads only
# ---------------------------------------------------------------------------


def _storefront() -> dict[str, str]:
    """The identities the live case needs, from the same local file scripts/catalog_ci.py
    reads. Never committed: a public test names no wallet and no store."""
    path = Path(
        os.environ.get("GECKO_CI_CONFIG", "~/.gecko/catalog-ci.json")
    ).expanduser()
    stored = json.loads(path.read_text()) if path.exists() else {}
    resolved = {
        key: os.environ.get(f"GECKO_CI_{key.upper()}") or stored.get(key, "")
        for key in ("buyer", "authority", "store")
    }
    missing = sorted(key for key, value in resolved.items() if not value)
    if missing:
        pytest.skip(
            f"no local storefront config ({', '.join(missing)} unset) — see {path}"
        )
    return resolved


@pytest.mark.rpc
@pytest.mark.bun
@needs_engine
@pytest.mark.skipif(
    not os.environ.get("GECKO_BRIDGE_RPC_URL"),
    reason="live lane is opt-in: set GECKO_BRIDGE_RPC_URL to a Solana RPC",
)
def test_the_projected_flow_simulates_on_chain_for_exactly_the_measured_compute() -> (
    None
):
    """The whole claim, end to end and unsigned: our graph -> our FDL -> HIS compiler
    -> HIS interpreter -> a real mainnet simulation, charging the compute this
    program is already known to charge. Nothing is signed and nothing is submitted."""
    storefront = _storefront()
    outcome = run_fdl(
        make_purchase_doc(),
        {
            "projectId": let_me_buy_project().project_id,
            "signer": storefront["buyer"],
            "authority": storefront["authority"],
            "mint": USDC,
            "store_name": storefront["store"],
            "product_name": os.environ.get("GECKO_CI_PRODUCT", "Water"),
            "table_number": 9,
        },
        rpc_url=os.environ["GECKO_BRIDGE_RPC_URL"],
    )

    assert outcome.ok, outcome.error
    transaction = outcome.node_outputs["tx"]["transactions"][0]
    assert transaction["unsignedTransaction"]
    simulation = transaction["simulation"]
    assert simulation["success"], simulation["logs"]
    # This program's compute tracks the bytes resident in the store's receipts vec,
    # which is capped — so the number is exact for a store at the cap and lower for a
    # new one. Override it for a storefront that is not the one it was measured on.
    expected_cu = int(os.environ.get("GECKO_BRIDGE_EXPECTED_CU", MAKE_PURCHASE_CU))
    assert simulation["computeUnits"] == expected_cu


def test_a_project_id_that_is_not_a_uuid_still_resolves() -> None:
    """The catalogue uses TWO id formats and our regex only knew one.

    `_PROJECT_ID` was `[0-9a-fA-F-]{8,}` — hex and dashes, written for the UUID-shaped ids
    the newer catalogue entries carry. jurassic_fi is `d2decec3-acdf-4946-…` and matched.
    let_me_buy is `p7o7nf4pucllzadrmiqhf`, which contains `p`, `n`, `u` and `z`, and did
    not — so every generic tool refused it with "the catalogue could not resolve
    BUYux…: it may not be indexed", while `find_start`, `list_stores` and
    `program_lifecycle` all knew the program perfectly well.

    Measured against the live catalogue: 49 of 4,499 programs carry a non-hex project id.
    That is 1.1%, and it contains **SPL Token, Associated Token Account, Token 2022,
    Pumpfun AMM, Meteora AMM, Meteora DLMM and Let Me Buy** — the oldest-indexed and most
    depended-upon entries. The blast radius of a parsing assumption is not proportional to
    how often it fires.
    """
    from gecko.orquestra_build import resolve_project_id

    class _Catalogue:
        def call_tool(self, _name: str, _args: dict) -> str:
            return (
                "Found 1 program(s) — sorted by weekly active users:\n\n"
                "- **Let Me Buy** (projectId: `p7o7nf4pucllzadrmiqhf`)\n"
                "  Program: `BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya`\n"
            )

    assert (
        resolve_project_id(_Catalogue(), "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya")
        == "p7o7nf4pucllzadrmiqhf"
    )


def test_a_uuid_project_id_still_resolves() -> None:
    """The other half, so the widened pattern cannot have broken what already worked."""
    from gecko.orquestra_build import resolve_project_id

    class _Catalogue:
        def call_tool(self, _name: str, _args: dict) -> str:
            return (
                "- **Jurassic** (projectId: `d2decec3-acdf-4946-bbb7-252a3c14ce2c`)\n"
            )

    assert (
        resolve_project_id(_Catalogue(), "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm")
        == "d2decec3-acdf-4946-bbb7-252a3c14ce2c"
    )


def test_a_response_with_no_project_id_still_refuses() -> None:
    """Widening the charset must not turn "not indexed" into a silent wrong answer."""
    import pytest

    from gecko.orquestra_build import OrquestraBuildError, resolve_project_id

    class _Empty:
        def call_tool(self, _name: str, _args: dict) -> str:
            return "No programs found matching your query."

    with pytest.raises(OrquestraBuildError, match="no project id"):
        resolve_project_id(_Empty(), "11111111111111111111111111111111")
