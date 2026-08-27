"""Auto-comprehend-on-pick — THE DIFFERENTIAL PROOF.

For each of the four wired programs, the config GENERATED from Orquestra's
captured surface + our source-recovered gap seeds + the explicit manual overlay
must EQUAL the hand-authored packaged config exactly. Without the overlay the
differential fails LOUDLY, listing exactly which knowledge remains
hand-supplied — that list quantifies what comprehension beyond the surface adds.

Honesty rules under test: a dropped/unrecoverable seed stays a flagged resolver
(never fabricated) and the FLAGGED state survives the merge; provenance is
recorded per PDA (extracted / recovered / manual).
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from importlib import resources
from pathlib import Path

import pytest

from gecko.orquestra_client import ProjectSurface
from gecko.orquestra_comprehend import (
    MAX_SOURCE_BYTES,
    ComprehendError,
    comprehend_project,
    diff_configs,
)
from gecko.providers.cli import comprehend_main

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"

# api_id -> (orquestra slug, curated source slice or None). Pump.fun is
# closed-source: IDL + overlay only.
CASES: dict[str, tuple[str, Path | None]] = {
    "meteora": (
        "v48gsz901w84zriqe0elsl",
        FIXTURES / "source" / "meteora_dlmm_pdas.rs",
    ),
    "pumpfun": ("6i6q26bmm46b89xlxo1kv", None),
    "ore": (
        "6alwvs9936laepljczqumb",
        Path("gecko") / "examples" / "ore_program_pdas.rs",
    ),
    "metadao_ico": (
        "krhmrxpy2fgwn3q0whic7",
        FIXTURES / "source" / "metadao_v07_launchpad_pdas.rs",
    ),
}

# The manual-overlay recipe list per program — the comprehension-beyond-the-
# surface artifact this deliverable exists to quantify. Pinned on purpose:
# shrinking it (more auto-derived) is progress; growing it silently is a
# regression in either the generator or our honesty.
EXPECTED_MANUAL: dict[str, tuple[str, ...]] = {
    "meteora": ("user_token",),
    # `bonding_curve` joined this list when the extractor stopped answering an account
    # its IDL declares two derivable ways: 14 instructions seed it on `mint` and 4 v2
    # instructions on `base_mint`, no instruction carries both names, and nothing on the
    # surface says the two denote the same token. The surface has not got worse — it was
    # always ambiguous and the answer used to come from IDL ORDER. The overlay asserts
    # `mint`, the spelling cross-verified against mainnet.
    "pumpfun": (
        "bonding_curve",
        "bonding_curve_v2",
        "creator_vault",
        "associated_bonding_curve",
        "associated_user",
    ),
    # the claim intent adds the two ATAs the live instruction surface reports
    # `pda: null` for — the claimer's ORE ATA and the treasury PDA's own ATA
    # (a two-step derivation no surface expresses).
    "ore": (
        "automation",
        "miner",
        "round",
        "stake",
        "recipient",
        "treasury_tokens",
    ),
    # the fund intent's two beyond-surface recipes: the #[event_cpi] macro PDA
    # (implicit in source — no visible seeds text) and the funder's USDC ATA.
    "metadao_ico": ("event_authority", "funder_quote_account"),
}


def _packaged(name: str) -> dict:
    anchor = resources.files("gecko.providers.configs").joinpath("orquestra", name)
    return json.loads(anchor.read_text(encoding="utf-8"))


def _surface(slug: str) -> ProjectSurface:
    pda = json.loads((FIXTURES / slug / "pda.json").read_text(encoding="utf-8"))
    idl = json.loads((FIXTURES / slug / "idl.json").read_text(encoding="utf-8"))
    return ProjectSurface(
        slug=slug,
        program_id=pda["programId"],
        idl=idl["idl"],
        pda_accounts=tuple(pda["pdaAccounts"]),
    )


def _comprehend(api_id: str, *, with_overlay: bool = True):
    slug, source_path = CASES[api_id]
    source = source_path.read_text(encoding="utf-8") if source_path else None
    overlay = _packaged(f"overlays/{api_id}.json") if with_overlay else None
    return comprehend_project(
        _surface(slug), api_id=api_id, source=source, overlay=overlay
    )


# -- the differential -------------------------------------------------------


@pytest.mark.parametrize("api_id", sorted(CASES))
def test_generated_plus_overlay_equals_hand_authored_exactly(api_id: str) -> None:
    """The whole proof: auto-comprehend reproduces the hand-authored config."""
    result = _comprehend(api_id)
    deltas = diff_configs(result.config, _packaged(f"{api_id}.json"))
    assert deltas == [], "generated config differs from hand-authored:\n" + "\n".join(
        deltas
    )


@pytest.mark.parametrize("api_id", sorted(CASES))
def test_manual_overlay_list_is_exactly_the_pinned_artifact(api_id: str) -> None:
    result = _comprehend(api_id)
    assert result.manual == EXPECTED_MANUAL[api_id]


@pytest.mark.parametrize("api_id", sorted(CASES))
def test_config_carries_the_computed_pda_origins(api_id: str) -> None:
    """R7: the tier is computed at the merge and EMITTED on the artifact
    (``program.pda_origins``) — no longer computed-then-discarded, which forced
    find_start to re-derive it from hand-maintained maps in a second place."""
    result = _comprehend(api_id)
    assert result.config["program"]["pda_origins"] == {
        name: prov.tier for name, prov in result.provenance.items()
    }


def test_without_overlay_the_differential_fails_loudly_naming_the_gaps() -> None:
    """Run pump.fun with NO overlay: the diff must list exactly the hand-supplied
    knowledge — the hidden account, the ATA recipes, the curated intents/notes."""
    result = _comprehend("pumpfun", with_overlay=False)
    hand = _packaged("pumpfun.json")
    hand_pdas = hand["program"]["pdas"]
    generated_pdas = result.config["program"]["pdas"]

    # absent from every machine surface: the IDL only *mentions* it in doc text
    assert "bonding_curve_v2" not in generated_pdas
    assert "associated_user" not in generated_pdas

    deltas = diff_configs(
        {k: generated_pdas.get(k) for k in hand_pdas}, hand_pdas, path="pdas"
    )
    named = "\n".join(deltas)
    for gap in EXPECTED_MANUAL["pumpfun"]:
        assert gap in named, f"differential did not name hand-supplied gap {gap!r}"
    # and the surface-derivable recipes carry NO delta at all (delimiter-aware:
    # `pdas.global` must not match a longer name that starts the same way).
    # `bonding_curve` left this list on purpose — see EXPECTED_MANUAL above; the
    # surface declares it two ways and no longer answers for the whole program.
    for clean in ("global", "mint_authority", "fee_config", "user_volume_accumulator"):
        assert not any(
            f"pdas.{clean}." in line or f"pdas.{clean} " in line for line in deltas
        ), f"surface-derivable recipe {clean!r} unexpectedly differs"


def test_metadao_core_seed_set_needs_no_manual_recipes() -> None:
    """The IDL declares zero PDAs; source recovery alone rebuilds every #[account(
    seeds=...)] recipe (launch/launch_signer/funding_record). The fund intent adds
    exactly two manual recipes — the #[event_cpi] macro PDA (implicit in source,
    no visible seeds text) and the funder's USDC ATA — declared in the overlay,
    never silently."""
    result = _comprehend("metadao_ico")
    assert result.manual == ("event_authority", "funder_quote_account")
    for name in ("launch", "launch_signer", "funding_record"):
        assert result.provenance[name].tier == "recovered"
    assert {p.tier for p in result.provenance.values()} == {"recovered", "manual"}


# -- honesty: flags survive, provenance is truthful -------------------------


def test_flagged_resolver_survives_with_no_overlay() -> None:
    """pump creator_vault: the seed reads bonding_curve.creator (runtime data).
    With no source and no overlay it stays an honest flagged resolver — never
    fabricated, never dropped."""
    result = _comprehend("pumpfun", with_overlay=False)
    prov = result.provenance["creator_vault"]
    assert prov.tier == "extracted"
    assert prov.flagged is True
    assert prov.unresolved == ("creator",)
    spec = result.config["program"]["pdas"]["creator_vault"]
    assert spec["seeds"][1]["kind"] == "resolver"


def test_flagged_state_survives_the_overlay_merge() -> None:
    """ore round: source recovers the seed SHAPE (id u64 LE) but the overlay
    re-flags it (the value is board.round_id — account data, not free choice).
    The final artifact keeps the flag: manual AND flagged."""
    result = _comprehend("ore")
    prov = result.provenance["round"]
    assert prov.tier == "manual"
    assert prov.flagged is True
    assert prov.unresolved == ("round_id",)


def test_provenance_tiers_meteora() -> None:
    """lb_pair/reserve are the #4057-dropped roots (source-recovered); the rest
    of the graph is IDL-extracted; user_token is overlay-supplied."""
    result = _comprehend("meteora")
    tiers = {name: p.tier for name, p in result.provenance.items()}
    assert tiers["lb_pair"] == "recovered"
    assert tiers["reserve"] == "recovered"
    assert tiers["oracle"] == "extracted"
    assert tiers["bin_array"] == "extracted"
    assert tiers["user_token"] == "manual"
    assert result.flagged == ()


def test_cross_program_pda_program_is_extracted_not_defaulted() -> None:
    """pump fee_config derives under the fee program — the IDL pins fee_program's
    address and the extraction honors it instead of defaulting to the owning
    program (the silent-wrong-address class)."""
    result = _comprehend("pumpfun", with_overlay=False)
    spec = result.config["program"]["pdas"]["fee_config"]
    assert spec["program_id"] == "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
    # ...and the hardcoded 32-byte const seed round-trips as a base58 pubkey
    assert spec["seeds"][1] == {
        "kind": "constant",
        "value": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "encoding": "pubkey",
    }


def test_unpinnable_cross_program_is_flagged_never_defaulted() -> None:
    """A pda.program of kind=account with NO pinned address: the derivation
    program is honestly unknown (None) and the recipe is flagged."""
    idl = {
        "address": "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv",
        "instructions": [
            {
                "name": "do",
                "accounts": [
                    {
                        "name": "foreign",
                        "pda": {
                            "seeds": [{"kind": "const", "value": [102, 111, 111]}],
                            "program": {"kind": "account", "path": "mystery_program"},
                        },
                    },
                    {"name": "mystery_program"},  # no pinned address
                ],
                "args": [],
            }
        ],
    }
    surface = ProjectSurface(
        slug="synthetic",
        program_id="oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv",
        idl=idl,
        pda_accounts=(),
    )
    result = comprehend_project(surface, api_id="synthetic")
    assert result.config["program"]["pdas"]["foreign"]["program_id"] is None
    assert result.provenance["foreign"].flagged is True


def test_legacy_idl_without_address_is_stamped_with_surface_program_id() -> None:
    """A legacy-shaped IDL (no address anywhere) still yields nodes under the
    project's program id — the /pda endpoint's programId is authoritative."""
    idl = {
        "version": "0.1.0",
        "name": "legacy",
        "instructions": [
            {
                "name": "init",
                "accounts": [
                    {
                        "name": "state",
                        "pda": {"seeds": [{"kind": "const", "value": [115]}]},
                    }
                ],
                "args": [],
            }
        ],
    }
    program_id = "moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM"
    surface = ProjectSurface(
        slug="legacy", program_id=program_id, idl=idl, pda_accounts=()
    )
    result = comprehend_project(surface, api_id="legacy")
    assert result.config["program"]["pdas"]["state"]["program_id"] == program_id


# -- overlay contract + untrusted-input caps --------------------------------


def test_overlay_unknown_key_is_refused() -> None:
    with pytest.raises(ComprehendError, match="unknown key"):
        _overlay_run({"fabricate": True})


def test_overlay_include_naming_unknown_account_is_refused() -> None:
    with pytest.raises(ComprehendError, match="unknown account"):
        _overlay_run({"include": ["not_a_real_account"]})


def test_overlay_why_must_map_names_to_reasons() -> None:
    with pytest.raises(ComprehendError, match="why"):
        _overlay_run({"why": {"x": 42}})


def test_overlay_intents_must_be_strings() -> None:
    with pytest.raises(ComprehendError, match="intents"):
        _overlay_run({"intents": [1, 2]})


def _overlay_run(overlay: dict) -> None:
    slug, _ = CASES["metadao_ico"]
    comprehend_project(_surface(slug), api_id="metadao_ico", overlay=overlay)


def test_oversized_source_is_refused() -> None:
    slug, _ = CASES["metadao_ico"]
    with pytest.raises(ComprehendError, match="source text exceeds"):
        comprehend_project(
            _surface(slug),
            api_id="metadao_ico",
            source="a" * (MAX_SOURCE_BYTES + 1),
        )


# -- CLI (thin wrapper; the engine is the product) --------------------------


class _FakeClient:
    """A stand-in OrquestraClient backed by the captured fixtures."""

    def list_projects(self, page: int = 1):  # noqa: ANN201 - duck-typed
        from gecko.orquestra_client import ProjectCatalogPage, ProjectInfo

        return ProjectCatalogPage(
            projects=(
                ProjectInfo(
                    id="v48gsz901w84zriqe0elsl",
                    name="Meteora DLLM",
                    program_id="LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
                ),
            ),
            page=page,
            total_pages=1,
            total=1,
        )

    def fetch_surface(self, slug: str) -> ProjectSurface:
        return _surface(slug)


def test_cli_comprehend_reproduces_the_packaged_config(tmp_path: Path) -> None:
    slug, source_path = CASES["meteora"]
    overlay_path = tmp_path / "overlay.json"
    overlay_path.write_text(
        json.dumps(_packaged("overlays/meteora.json")), encoding="utf-8"
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = comprehend_main(
            [
                "--project",
                slug,
                "--api-id",
                "meteora",
                "--source",
                str(source_path),
                "--overlay",
                str(overlay_path),
            ],
            client=_FakeClient(),
        )
    assert rc == 0
    assert json.loads(out.getvalue()) == _packaged("meteora.json")
    # provenance goes to stderr, config to stdout — pipeable, no file writes
    assert "lb_pair: recovered" in err.getvalue()
    assert "user_token: manual" in err.getvalue()


def test_cli_list_prints_the_catalog() -> None:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = comprehend_main(["--list"], client=_FakeClient())
    assert rc == 0
    assert "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo" in out.getvalue()


def test_cli_requires_project_or_list() -> None:
    with pytest.raises(SystemExit):
        comprehend_main([], client=_FakeClient())


def test_cli_reports_engine_errors_without_traceback(tmp_path: Path) -> None:
    bad_overlay = tmp_path / "bad.json"
    bad_overlay.write_text('{"fabricate": true}', encoding="utf-8")
    slug, _ = CASES["meteora"]
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = comprehend_main(
            ["--project", slug, "--overlay", str(bad_overlay)],
            client=_FakeClient(),
        )
    assert rc == 2
    assert "unknown key" in err.getvalue()


def test_gecko_orquestra_comprehend_subcommand_dispatches() -> None:
    """`gecko orquestra comprehend --help` reaches the comprehend parser."""
    from gecko.providers.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["comprehend", "--help"])
    assert exc.value.code == 0


# -- live smoke (env-gated; live data drifts — the drift series owns equality) --


@pytest.mark.skipif(
    os.getenv("GECKO_ORQUESTRA_LIVE") != "1",
    reason="set GECKO_ORQUESTRA_LIVE=1 to hit the live Orquestra catalog",
)
def test_live_catalog_and_generator_run() -> None:
    from gecko.orquestra_client import OrquestraClient

    client = OrquestraClient()
    page = client.list_projects()
    assert page.projects  # the catalog answers

    slug, _ = CASES["meteora"]
    surface = client.fetch_surface(slug)
    result = comprehend_project(surface, api_id="meteora")
    # the generator runs against live data; NOT an equality assert on purpose
    assert result.config["program"]["program_id"] == surface.program_id
    assert result.config["program"]["pdas"]
