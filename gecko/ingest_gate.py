"""Should this program be ingested at all? A gate on ingesting, not a step in ingesting.

WHY THIS IS A GATE AND NOT A STEP. Ingestion changes nothing when the program is fine —
the config lands either way. This module earns its place only when it REFUSES: "do not
ship whirlpool, it declares an intent nothing supplies, so it will have no start card, no
``list_programs`` row and no drift target, and NOTHING will say so." That is not a
hypothetical. ``gecko/providers/configs/orquestra/whirlpool.json`` declares
``"intents": ["plan_swap"]``; :func:`gecko.providers.cli.intent_registries` returns
``{jupiter, metadao_ico, meteora, ore, pumpfun}``; the orphan intent is dropped in silence
at ``gecko/providers/cli.py:68`` and again at ``gecko/find_start.py:966``. It shipped. The
whole value of this module is saying no BEFORE that happens again.

THREE OUTCOMES PLUS A DEGRADE, BECAUSE "NO DATA" IS NOT "GOOD DATA". :mod:`gecko.peg_guard`
already models the split and this module follows its shape, adding the one distinction an
ingest decision needs that a peg reading does not — *broken now* is a different fact from
*breaks later*:

* ``ok``      — measured, and the measurement says go.
* ``refuse``  — measured, and it will be BROKEN: an unreachable start, an offset computed
                from a discriminator width that is not this program's, an account whose
                address cannot be reached from anything a caller can name.
* ``warn``    — measured, and it will DEGRADE: it works today at the size we tested and
                stops working at a size we did not. ``read_accounts`` has no ``where``
                predicate and refuses above ``MAX_TOOL_INSTANCES = 50``
                (``gecko/read_accounts.py:79``), so "one per NFT mint" is a working path
                until the 51st mint exists.
* ``unknown`` — NOT MEASURED. No packaged IDL, an empty catalog, nothing to compare
                against. Collapsing this into ``ok`` is the fail-open shape this repo
                keeps finding: a control that silently ceases to exist exactly when it
                would have mattered. Three of the eight programs have no offline IDL at
                all, and the honest answer for them is that the question was not asked.

EVERY REFUSAL NAMES THE FILE, THE MISSING THING, AND THE ONE ACTION. A finding that says
only "failed" is a bug in this module. :class:`Finding` cannot be constructed without a
``location`` (the file, and the line where it is stable), a ``missing`` (the fact that is
absent) and a ``fix`` (the single edit that closes it).

OFFLINE AND DETERMINISTIC. Every check reads a packaged config, an IDL handed in by the
caller, or the source of a dispatch table in this repo. No RPC, no network, no model call,
no clock. Run it twice, get the same bytes.

WHAT IS DELIBERATELY NOT HERE. Only checks that were measured across the eight existing
programs and that actually DISCRIMINATE between them are implemented. A check every
program passes is not a gate, it is a comment.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

__all__ = [
    "CHECKS",
    "CheckResult",
    "Finding",
    "GateReport",
    "Outcome",
    "check_cardinality",
    "check_discrimination",
    "check_framework_fingerprint",
    "check_intent_reachability",
    "check_registry_consistency",
    "gate",
    "precheck_config",
    "RECORDED_REFUSALS",
    "render",
]

#: ``warn`` is the member :mod:`gecko.peg_guard` does not need. A peg either holds or it
#: does not; an ingest can be correct at 8 accounts and wrong at 80.
Outcome = Literal["ok", "refuse", "warn", "unknown"]

#: Worst-first. The report's outcome is the worst any check returned, and ``unknown``
#: ranks ABOVE ``ok``: a question we did not ask must not read as a question that passed.
_SEVERITY: dict[Outcome, int] = {"ok": 0, "unknown": 1, "warn": 2, "refuse": 3}

#: The checks this gate runs, in the order a reader should meet them.
CHECKS: tuple[str, ...] = (
    "intent-reachability",
    "registry-consistency",
    "framework-fingerprint",
    "cardinality",
    "discrimination",
)


#: Anchor's width, mirrored from :data:`gecko.idl_layout.ANCHOR_DISCRIMINATOR_LEN` by
#: import rather than by retyping — a gate that guards an assumption must fail when the
#: assumption moves.
def _anchor_width() -> int:
    from .idl_layout import ANCHOR_DISCRIMINATOR_LEN

    return ANCHOR_DISCRIMINATOR_LEN


def _instance_cap() -> int:
    from .read_accounts import MAX_TOOL_INSTANCES

    return MAX_TOOL_INSTANCES


@dataclass(frozen=True)
class Finding:
    """One located, fixable fact. The three fields after ``check`` are not optional by
    accident: a refusal that cannot name where it is, what is absent, and what to do about
    it is indistinguishable from a shrug."""

    check: str
    outcome: Outcome
    #: file (and line, where the line is stable) the missing thing is missing FROM.
    location: str
    #: the thing that is absent, named specifically enough to grep for.
    missing: str
    #: the ONE action that closes it.
    fix: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "outcome": self.outcome,
            "location": self.location,
            "missing": self.missing,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class CheckResult:
    """What one check measured, and whether that is a reason to stop."""

    name: str
    outcome: Outcome
    #: what was measured, in the check's own words — including, for
    #: ``discrimination``, the statement of what it does NOT measure.
    headline: str
    findings: tuple[Finding, ...] = ()
    #: the numbers behind the headline, so a caller can diff two runs without re-parsing
    #: prose. Categorical and structural only — never an account's contents.
    measured: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        """Only ``refuse`` stops an ingest. ``warn`` and ``unknown`` are reported and
        never enforced — a gate that blocks on every unmeasured question blocks every
        program whose IDL is fetched live, which is three of the eight we already ship."""
        return self.outcome == "refuse"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "headline": self.headline,
            "measured": dict(self.measured),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class GateReport:
    program: str
    provider: str
    outcome: Outcome
    checks: tuple[CheckResult, ...]
    summary: str

    @property
    def blocks(self) -> bool:
        return self.outcome == "refuse"

    def check(self, name: str) -> CheckResult:
        for result in self.checks:
            if result.name == name:
                return result
        raise KeyError(f"no check named {name!r}; this gate runs {list(CHECKS)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "program": self.program,
            "provider": self.provider,
            "outcome": self.outcome,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


def _worst(outcomes: Iterable[Outcome]) -> Outcome:
    worst: Outcome = "ok"
    for outcome in outcomes:
        if _SEVERITY[outcome] > _SEVERITY[worst]:
            worst = outcome
    return worst


def _config_path(provider: str, api_id: str) -> str:
    """The packaged path a finding points at. A relative repo path, because that is what
    a reader opens; the loader itself goes through ``importlib.resources``."""
    return f"gecko/providers/configs/{provider}/{api_id}.json"


# --------------------------------------------------------------------------- #
# 1 — intent-reachability
# --------------------------------------------------------------------------- #
def check_intent_reachability(
    api_id: str,
    declared: Sequence[str],
    supplied: Mapping[str, Mapping[str, Any]],
    started: Mapping[str, Mapping[str, Any]],
    *,
    provider: str = "orquestra",
    overlay_declared: Sequence[str] = (),
) -> CheckResult:
    """Does something SUPPLY every intent this config declares?

    ``declared`` is ``program.intents`` from the packaged config. ``supplied`` is
    :func:`gecko.providers.cli.intent_registries` — api_id → intent name → plan callable.
    ``started`` is :func:`gecko.providers.cli.start_specs`, the declarative half the
    router needs to build a start card. An intent named in the config with no entry in
    EITHER is an orphan, and both consumers drop it without a word: ``providers/cli.py:68``
    filters it out of the served surface, ``find_start.py:966`` ``continue``s past it.

    A program that declares ``"intents": []`` passes VACUOUSLY, and that is not a
    loophole: ``let_me_buy`` and ``jurassic_fi`` declare none on purpose (comprehended for
    derivation, no execute intent wired), so there is no orphan to find. The vacuity is
    recorded in ``measured`` rather than hidden, because "nothing declared" and "everything
    supplied" are different facts.
    """
    wanted = list(dict.fromkeys(list(declared) + list(overlay_declared)))
    have_intents = set((supplied.get(api_id) or {}))
    have_starts = set((started.get(api_id) or {}))
    findings: list[Finding] = []
    missing_intents = [name for name in wanted if name not in have_intents]
    missing_starts = [
        name for name in wanted if name in have_intents and name not in have_starts
    ]
    for name in missing_intents:
        where = (
            "no key for this api_id at all"
            if api_id not in supplied
            else f"key {api_id!r} exists but does not carry {name!r}"
        )
        findings.append(
            Finding(
                "intent-reachability",
                "refuse",
                "gecko/providers/cli.py:77 intent_registries()",
                f"{api_id}.{name} — {where}. The config declares it at "
                f"{_config_path(provider, api_id)} · program.intents",
                f"add {api_id!r} → {{{name!r}: ...}} to intent_registries() (and its "
                f"StartSpec to start_specs() at gecko/providers/cli.py:94), or delete "
                f"{name!r} from the config. Today it is dropped in silence at "
                "gecko/providers/cli.py:68 and gecko/find_start.py:966, so the program "
                "ships with no start card, no list_programs row and no drift target",
            )
        )
    for name in missing_starts:
        findings.append(
            Finding(
                "intent-reachability",
                "refuse",
                "gecko/providers/cli.py:94 start_specs()",
                f"{api_id}.{name} has a plan callable but no StartSpec",
                f"add {name!r} to start_specs()[{api_id!r}] — find_start builds the "
                "start card from the StartSpec, so without one the intent is callable "
                "but unroutable",
            )
        )
    outcome: Outcome = "refuse" if findings else "ok"
    supplied_names = sorted(n for n in wanted if n in have_intents and n in have_starts)
    missing_names = sorted(set(missing_intents) | set(missing_starts))
    if not wanted:
        headline = (
            f"{api_id}: declared={{}} supplied={{}} missing={{}} — the config declares "
            '"intents": [] explicitly (derivation-only program); vacuous pass'
        )
    else:
        headline = (
            f"{api_id}: declared={{{', '.join(sorted(wanted))}}} "
            f"supplied={{{', '.join(supplied_names)}}} "
            f"missing={{{', '.join(missing_names)}}}"
        )
    return CheckResult(
        "intent-reachability",
        outcome,
        headline,
        tuple(findings),
        {
            "declared": sorted(wanted),
            "supplied": supplied_names,
            "missing": missing_names,
            "vacuous": not wanted,
        },
    )


# --------------------------------------------------------------------------- #
# 2 — registry-consistency
# --------------------------------------------------------------------------- #
#: The six registries a program has to be in to be fully wired, and what each one buys.
#: They are NOT enforced in lockstep anywhere today — ``tests/test_catalog_surface.py``
#: compares ``list_programs`` against ``PROGRAMS``, which is what ``list_programs`` is
#: built from, so it is a tautology and cannot see a program missing from both.
_REGISTRIES: tuple[tuple[str, str], ...] = (
    ("R1", "listed in the provider's provider.json apis"),
    ("R2", "has a packaged <api_id>.json carrying a program block"),
    ("R3", "every declared intent is supplied (start cards exist)"),
    ("R4", "in providers/cli.PROGRAMS (--program target + list_programs row)"),
    ("R5", "has a drift_watch dispatch key (drift target)"),
    ("R6", "named by at least one row of find_start_golden.jsonl"),
)


def _dispatch_keys(module_path: Path, func_name: str) -> set[tuple[str, str]]:
    """The ``(program, instruction)`` pairs a dispatch function can route, read from its
    SOURCE with :mod:`ast`.

    Read rather than called, because calling it routes to an orchestrator that opens an
    RPC connection, and this module is offline by construction. Read with ``ast`` rather
    than a regex, because a regex over source is a guess and a parse is not.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    keys: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Tuple) or len(inner.elts) != 2:
                continue
            parts = [
                elt.value
                for elt in inner.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            if len(parts) == 2:
                keys.add((parts[0], parts[1]))
    return keys


def check_registry_consistency(
    api_id: str,
    *,
    provider: str = "orquestra",
    in_provider_json: bool,
    has_program_block: bool,
    intents_reachable: bool,
    in_programs: bool,
    drift_keys: Sequence[tuple[str, str]],
    golden_rows: int,
) -> CheckResult:
    """Is this program in every registry that has to know about it, or only some?

    Six registries, no lockstep between them, and a program can sit in four of six and
    look completely healthy from any one of them. The measured spread across the eight
    existing programs is 6/6 (pumpfun, meteora, ore) down to 2/6 (whirlpool) — which is
    why this check is here and why it is not a boolean.

    R1/R2 are load-bearing: without them nothing loads at all. R3 is reachability (the
    same fact :func:`check_intent_reachability` refuses on, counted here so the score is
    honest). R4/R5/R6 are DISCOVERY, DRIFT and REGRESSION COVER — a program missing them
    works when you call it by hand and is invisible, unwatched, or untested otherwise.
    That is the definition of degrade, so they warn.
    """
    drift_hit = [key for key in drift_keys if key[0].lower() == api_id.lower()]
    near_miss = [
        key
        for key in drift_keys
        if key not in drift_hit
        and (key[0].lower() in api_id.lower() or api_id.lower() in key[0].lower())
    ]
    present = {
        "R1": in_provider_json,
        "R2": has_program_block,
        "R3": intents_reachable,
        "R4": in_programs,
        "R5": bool(drift_hit),
        "R6": golden_rows > 0,
    }
    findings: list[Finding] = []
    if not present["R1"]:
        findings.append(
            Finding(
                "registry-consistency",
                "refuse",
                f"gecko/providers/configs/{provider}/provider.json · apis",
                f"{api_id!r} is not in the provider's apis list",
                f"add {api_id!r} to apis — load_packaged_provider reads that list and "
                "nothing else, so a config file nobody lists is a file nobody opens",
            )
        )
    if not present["R2"]:
        findings.append(
            Finding(
                "registry-consistency",
                "refuse",
                _config_path(provider, api_id),
                "no program block (kind != 'program', or program is null)",
                'set "kind": "program" and give it a program block with program_id + '
                "pdas — every downstream registry skips an api whose program is None",
            )
        )
    if not present["R3"]:
        findings.append(
            Finding(
                "registry-consistency",
                "refuse",
                "gecko/providers/cli.py:77 intent_registries()",
                f"{api_id} declares an intent nothing supplies (see intent-reachability)",
                "supply the intent or stop declaring it — this registry is the one that "
                "decides whether a start card exists at all",
            )
        )
    if not present["R4"]:
        findings.append(
            Finding(
                "registry-consistency",
                "warn",
                "gecko/providers/cli.py:135 PROGRAMS (built by _discover_programs at :111)",
                f"{api_id!r} is not a key of PROGRAMS",
                f"register {api_id}'s intents so _discover_programs picks it up. Until "
                "then there is no `gecko-orquestra --program "
                f"{api_id}` target and no row in list_programs "
                "(gecko/providers/catalog_surface.py:384 loops over PROGRAMS), so the "
                "program is reachable only by an agent that already knows its name",
            )
        )
    if not present["R5"]:
        hint = (
            f" A near-miss key exists: {near_miss[0]!r} — the dispatch spells the "
            f"program {near_miss[0][0]!r} while the api_id is {api_id!r}, so a target "
            f"named {api_id!r} raises WatchError. gecko/prove.py:89 already carries BOTH "
            "spellings; drift_watch does not."
            if near_miss
            else ""
        )
        findings.append(
            Finding(
                "registry-consistency",
                "warn",
                "gecko/drift_watch.py:211 _default_simulator",
                f"no dispatch key ({api_id!r}, <instruction>)." + hint,
                f"add a branch keyed ({api_id!r}, <instruction>) pointing at a "
                f"gecko/providers/{api_id}_landing.py orchestrator. Without one this "
                "program can never be a drift target, so a surface change lands "
                "unobserved",
            )
        )
    if not present["R6"]:
        findings.append(
            Finding(
                "registry-consistency",
                "warn",
                f"gecko/providers/configs/{provider}/find_start_golden.jsonl",
                f"0 rows name gold_program {api_id!r}",
                f"add at least one golden row for {api_id} — the golden set is the only "
                "thing that catches a routing regression, and a program with no rows "
                "cannot regress visibly",
            )
        )
    score = sum(1 for value in present.values() if value)
    outcome = _worst(f.outcome for f in findings) if findings else "ok"
    detail = ", ".join(
        f"{key}{'+' if value else '-'}" for key, value in sorted(present.items())
    )
    return CheckResult(
        "registry-consistency",
        outcome,
        f"{api_id}: {score}/6 registries — {detail}"
        + (f" (drift keys: {sorted(drift_hit)})" if drift_hit else "")
        + (f" ({golden_rows} golden rows)" if golden_rows else ""),
        tuple(findings),
        {
            "score": score,
            "of": 6,
            "present": {key: bool(value) for key, value in present.items()},
            "drift_keys": sorted(drift_hit),
            "near_miss_drift_keys": sorted(near_miss),
            "golden_rows": golden_rows,
        },
    )


# --------------------------------------------------------------------------- #
# 3 — framework-fingerprint
# --------------------------------------------------------------------------- #
def _section_widths(idl: Mapping[str, Any], section: str) -> dict[str, int | None]:
    """entry name → declared discriminator length, ``None`` when it declares none."""
    out: dict[str, int | None] = {}
    for entry in idl.get(section) or ():
        if not isinstance(entry, Mapping):
            continue
        raw = entry.get("discriminator")
        out[str(entry.get("name"))] = None if not raw else len(list(raw))
    return out


def check_framework_fingerprint(
    api_id: str, idl: Mapping[str, Any] | None, *, idl_source: str = "the supplied IDL"
) -> CheckResult:
    """Is this program's account prefix the width our arithmetic assumes?

    ``gecko/idl_layout.py:51`` fixes ``ANCHOR_DISCRIMINATOR_LEN = 8`` and ``field_offset``
    (:159) and ``account_size`` (:219) start their arithmetic at it UNCONDITIONALLY —
    neither consults the discriminator the IDL actually declares. On an Anchor program
    that is right. On ``ore`` (``metadata.origin: "steel"``, 15 instructions carrying a
    ONE-byte discriminator) it is not, and the failure mode is the one this repo cares
    about most: no exception, no warning, an offset 7 bytes late that decodes the
    neighbouring field into a well-formed, resolvable, WRONG address.

    A legacy pre-0.30 Anchor IDL (``metadao_ico``: 0 of 13 instructions, 0 of 4 accounts
    and 0 of 11 events declare one) is the other shape. It does not decode wrongly — it
    refuses cleanly at ``account_discriminator`` — but nothing can be enumerated by
    memcmp, so it degrades rather than breaks.

    ``None`` for ``idl`` is ``unknown`` and never ``ok``: three of the eight programs we
    ship (jupiter, whirlpool, jurassic_fi) have no packaged IDL and fetch it live, so this
    question is genuinely unasked for them rather than answered in the affirmative.
    """
    width = _anchor_width()
    if idl is None:
        return CheckResult(
            "framework-fingerprint",
            "unknown",
            f"{api_id}: CANNOT MEASURE OFFLINE — no IDL was supplied, and this check "
            "compares an IDL's declared discriminator widths against "
            f"gecko/idl_layout.py:51 ANCHOR_DISCRIMINATOR_LEN = {width}. Not measured is "
            "not measured-good",
            (
                Finding(
                    "framework-fingerprint",
                    "unknown",
                    _config_path("orquestra", api_id),
                    "no IDL reachable offline for this program",
                    "pass --idl <path> (or package the IDL beside the config) and re-run "
                    "— until then no statement about this program's prefix width exists",
                ),
            ),
            {"measured": False},
        )

    metadata = idl.get("metadata") or {}
    sections = {
        section: _section_widths(idl, section)
        for section in ("instructions", "accounts", "events")
    }
    if not any(sections.values()):
        # NOTHING TO COMPARE IS NOT AGREEMENT. An IDL that declares no instruction,
        # account or event has no discriminator width to check, and this check used to
        # read that silence as a pass: hand the gate `{}` and it certified a program
        # whose real IDL it refuses. An empty document agrees with every constant we
        # could test it against, which is why it must read as unknown — exactly like the
        # absent IDL above, and for the same reason.
        return CheckResult(
            "framework-fingerprint",
            "unknown",
            f"{api_id}: an IDL was supplied ({idl_source}) but it declares no "
            "instructions, accounts or events, so there is no declared discriminator "
            f"width to compare against gecko/idl_layout.py:51 = {width} bytes. Not "
            "measured is not measured-good",
            (
                Finding(
                    "framework-fingerprint",
                    "unknown",
                    _config_path("orquestra", api_id),
                    "the supplied IDL is empty of every section this check reads "
                    "(instructions, accounts, events)",
                    "supply the program's real IDL — and if this IS its real IDL, the "
                    "program cannot be comprehended from it at all, so the config "
                    "should not be ingested on the strength of it",
                ),
            ),
            {"measured": False, "empty_idl": True},
        )
    declared_types = {
        str(entry.get("name"))
        for entry in idl.get("types") or ()
        if isinstance(entry, Mapping)
    }
    findings: list[Finding] = []

    for section, widths in sections.items():
        wrong = sorted(
            f"{name}@{value}B"
            for name, value in widths.items()
            if value not in (None, width)
        )
        if wrong:
            findings.append(
                Finding(
                    "framework-fingerprint",
                    "refuse",
                    f"gecko/idl_layout.py:51 ANCHOR_DISCRIMINATOR_LEN = {width}",
                    f"{len(wrong)} of {len(widths)} {section} declare a discriminator "
                    f"that is not {width} bytes: {', '.join(wrong[:6])}"
                    + (" …" if len(wrong) > 6 else ""),
                    f"do not ingest {api_id} until field_offset (idl_layout.py:159) and "
                    "account_size (:219) read the DECLARED width instead of the "
                    f"constant. Today they add {width} regardless, so every offset for "
                    "these entries is silently off and decodes a neighbouring field",
                )
            )
    missing = sorted(
        name for name, value in sections["accounts"].items() if value is None
    )
    if missing:
        findings.append(
            Finding(
                "framework-fingerprint",
                "warn",
                "gecko/idl_layout.py:138 account_discriminator",
                f"{len(missing)} of {len(sections['accounts'])} accounts declare NO "
                f"discriminator ({', '.join(missing[:6])})"
                + (" …" if len(missing) > 6 else ""),
                "this IDL predates Anchor 0.30 — regenerate it with a modern Anchor, or "
                "accept that no account of this program can be found by memcmp "
                "(account_discriminator refuses, so read_accounts cannot enumerate)",
            )
        )
    inline = sorted(name for name in sections["accounts"] if name not in declared_types)
    if inline:
        findings.append(
            Finding(
                "framework-fingerprint",
                "warn",
                "gecko/idl_layout.py:84 _struct_fields (scans idl.types only)",
                f"{len(inline)} of {len(sections['accounts'])} account structs are inline "
                f"on the account entry, not in idl.types ({', '.join(inline[:6])})"
                + (" …" if len(inline) > 6 else ""),
                "lift these structs into idl.types (or hand-write the decoder, as "
                'gecko/ore_state.py does) — field_offset raises "is not declared in '
                'idl.types" for every one of them, so no field can be located',
            )
        )

    outcome = _worst(f.outcome for f in findings) if findings else "ok"
    framework = (
        f"origin={metadata.get('origin')}"
        if metadata.get("origin")
        else (
            f"Anchor spec={metadata.get('spec')}"
            if metadata.get("spec")
            else "legacy Anchor (no metadata.spec)"
        )
    )

    def _counts(widths: Mapping[str, int | None]) -> str:
        parts = [
            f"{sum(1 for v in widths.values() if v == w)}@{w}B"
            for w in sorted({v for v in widths.values() if v is not None})
        ]
        blank = sum(1 for v in widths.values() if v is None)
        if blank:
            parts.append(f"{blank}@none")
        return "/".join(parts)

    counts = ", ".join(
        f"{section} {_counts(widths)}" for section, widths in sections.items() if widths
    )
    named = " ".join(
        part
        for part in (
            str(metadata.get("name") or ""),
            f"v{metadata['version']}" if metadata.get("version") else "",
        )
        if part
    )
    return CheckResult(
        "framework-fingerprint",
        outcome,
        f"{api_id}: {framework}"
        + (f", {named}" if named else "")
        + f" ({idl_source}); {counts}; account structs in "
        f"idl.types {len(sections['accounts']) - len(inline)}/{len(sections['accounts'])}",
        tuple(findings),
        {
            "measured": True,
            "framework": framework,
            "assumed_width": width,
            "widths": {
                section: sorted(
                    {("none" if v is None else v) for v in widths.values()},
                    key=str,
                )
                for section, widths in sections.items()
            },
            "accounts_inline": inline,
            "accounts_without_discriminator": missing,
        },
    )


# --------------------------------------------------------------------------- #
# 4 — cardinality
# --------------------------------------------------------------------------- #
#: Seed encodings that are a NUMBER by construction. ``gecko/pda.py:74`` admits exactly
#: five encodings; these two settle the question on their own — the bytes are an integer.
_NUMERIC_ENCODINGS = frozenset({"le", "be"})

#: Seed encodings that are an IDENTITY by construction. A key is never a selector.
_IDENTITY_ENCODINGS = frozenset({"pubkey"})

# ``utf8`` and ``bytes`` are the other two, and they settle NOTHING. Orca writes
# ``start_tick_index`` — an i32 — as its decimal string; LetMeBuy writes ``store_name``,
# a name its caller chose, exactly the same way. Same encoding, opposite natures.
#
# This is the trap this whole check exists for, and the check used to fall into it: an
# earlier _NAMEABLE_ENCODINGS listed ``utf8`` as an identity, which made every
# number-written-as-text INVISIBLE to the one test meant to catch it — Whirlpool's
# ``tick_array.start_tick_index`` scored clean. (It also listed ``base58``, which is not
# a SeedEncoding at all: the set was written from intuition instead of from the type.)
#
# The encoding says how the bytes are MADE. Only the IDL's declared argument type says
# what the value IS. When there is no IDL, the honest answer is that we do not know —
# never that it is a name.
_NUMERIC_IDL_TYPES = frozenset(
    {
        "u8",
        "u16",
        "u32",
        "u64",
        "u128",
        "u256",
        "i8",
        "i16",
        "i32",
        "i64",
        "i128",
        "i256",
        "f32",
        "f64",
    }
)


def _seed_rows(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    seeds = node.get("seeds") or ()
    return [s for s in seeds if isinstance(s, Mapping)]


def _is_singleton(node: Mapping[str, Any]) -> bool:
    return all(seed.get("kind") == "constant" for seed in _seed_rows(node))


def _argument_types(idl: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """argument name → its declared IDL type, across every instruction that names one.

    ``None`` when there is no IDL — which the caller must propagate as *unknown*, not as
    a default. The PDA recipes carry no link back to the instruction an argument belongs
    to, so this is a lookup by name across the whole program; a name declared with two
    different types in two instructions keeps the first, and that ambiguity is itself
    worth knowing if it ever appears.
    """
    if idl is None:
        return None
    out: dict[str, Any] = {}
    for entry in idl.get("instructions") or ():
        if not isinstance(entry, Mapping):
            continue
        for arg in entry.get("args") or ():
            if isinstance(arg, Mapping) and arg.get("name"):
                out.setdefault(str(arg["name"]), arg.get("type"))
    return out


def _seed_nature(seed: Mapping[str, Any], arg_types: Mapping[str, Any] | None) -> str:
    """``"number"``, ``"identity"``, or ``"unknown"`` — never a guess."""
    if seed.get("source") == "account":
        return "identity"
    encoding = str(seed.get("encoding") or "pubkey")
    if encoding in _NUMERIC_ENCODINGS:
        return "number"
    if encoding in _IDENTITY_ENCODINGS:
        return "identity"
    if arg_types is None:
        return "unknown"
    declared = arg_types.get(str(seed.get("name")))
    if isinstance(declared, str):
        return "number" if declared in _NUMERIC_IDL_TYPES else "identity"
    return "unknown"


def _classify_argument_seeds(
    node: Mapping[str, Any], arg_types: Mapping[str, Any] | None
) -> tuple[list[str], list[str]]:
    """Split a recipe's argument seeds into ``(selectors, unclassified)``.

    A SELECTOR is a number sitting beside an identity: the same identity then yields one
    well-formed address per value of that number, and the surface says nothing about
    which value is real. Whirlpool's own overlay proves it twice — ``tick_spacing`` 2 and
    64 both derive resolvable pools, both wrong; ``start_tick_index`` does the same one
    layer down. A number on its OWN (a bare index) is not flagged: there is nothing for
    it to disambiguate.

    UNCLASSIFIED is the seed we cannot call either way — a ``utf8``/``bytes`` argument
    with no IDL to type it. It is returned separately and reported as ``unknown``,
    because folding it into "identity" is precisely how the Whirlpool seed hid.
    """
    rows = _seed_rows(node)
    natures = {
        id(seed): _seed_nature(seed, arg_types)
        for seed in rows
        if seed.get("kind") in ("variable", "resolver", "ordered_pair")
    }
    identities = [seed for seed in rows if natures.get(id(seed)) == "identity"]
    arguments = [
        seed
        for seed in rows
        if seed.get("kind") == "variable" and seed.get("source") == "argument"
    ]
    unclassified = [
        str(seed.get("name"))
        for seed in arguments
        if natures.get(id(seed)) == "unknown"
    ]
    if not identities:
        return [], unclassified
    selectors = [
        str(seed.get("name")) for seed in arguments if natures.get(id(seed)) == "number"
    ]
    return selectors, unclassified


def check_cardinality(
    api_id: str,
    pdas: Mapping[str, Mapping[str, Any]],
    *,
    idl: Mapping[str, Any] | None = None,
    program_id: str = "",
    provider: str = "orquestra",
) -> CheckResult:
    """Can a caller reach the ONE instance it means, or only enumerate?

    This is a CARDINALITY CLASS, computed from the surface. It is not an instance count —
    counting instances needs a chain, and this module never touches one.

    Three mechanical classes over the config's declared recipes:

    * SINGLETON — every seed is a constant. One address, derive it.
    * SELECTOR-SEEDED — an identity seed plus a numeric ARGUMENT seed. Every value of the
      number derives a different, equally well-formed address (``whirlpool`` on
      ``tick_spacing``, ``lb_pair`` on ``bin_step``/``base_factor`` — the fee-tier trap
      that is derivable from nothing on the surface in any AMM we have read).
    * RESOLVER-SEEDED — a value that must be READ off another account. It has a path when
      every ``depends_on`` names a recipe this same config declares (``ore``'s ``round``
      through the singleton ``board``; ``pumpfun``'s ``creator_vault`` through
      ``bonding_curve``), and no path at all when it names something undeclared.

    When an IDL is supplied, the account TYPES are checked too: a declared type with
    neither a config recipe nor a witness from
    :func:`gecko.account_recipes.verification_recipe` has no read path, and
    ``read_accounts`` refuses ``no-verification-recipe`` on it.

    Why the selector and the uncovered type WARN rather than REFUSE: both leave a working
    path (name the number, or enumerate) that stops working at a size nobody chose.
    ``read_accounts`` has no ``where`` predicate and refuses above
    ``MAX_TOOL_INSTANCES`` (``gecko/read_accounts.py:79``, 50 today), so the ceiling is
    real and near — the exact number is read from that module and printed in the headline.
    """
    findings: list[Finding] = []
    singletons: list[str] = []
    selectors: dict[str, list[str]] = {}
    unclassified: dict[str, list[str]] = {}
    resolver_paths: dict[str, list[str]] = {}
    arg_types = _argument_types(idl)
    for name, node in sorted(pdas.items()):
        if not isinstance(node, Mapping):
            continue
        if _is_singleton(node):
            singletons.append(name)
        found, unknown_seeds = _classify_argument_seeds(node, arg_types)
        if found:
            selectors[name] = found
        if unknown_seeds:
            unclassified[name] = unknown_seeds
        for seed in _seed_rows(node):
            if seed.get("kind") != "resolver":
                continue
            parents = [str(p) for p in (seed.get("depends_on") or ())]
            resolver_paths[name] = parents
            orphans = [p for p in parents if p not in pdas]
            if orphans:
                findings.append(
                    Finding(
                        "cardinality",
                        "refuse",
                        f"{_config_path(provider, api_id)} · program.pdas.{name}",
                        f"seed {seed.get('name')!r} resolves from "
                        f"{', '.join(orphans)}, which this config does not declare",
                        f"declare a recipe for {orphans[0]!r} (or drop the {name!r} "
                        "recipe) — a resolver whose parent has no recipe has no path at "
                        "all: the address cannot be derived offline and cannot be read "
                        "online either",
                    )
                )
    for name, numbers in sorted(selectors.items()):
        findings.append(
            Finding(
                "cardinality",
                "warn",
                f"{_config_path(provider, api_id)} · program.pdas.{name}",
                f"selector-seeded: {', '.join(sorted(numbers))} "
                f"{'is a number' if len(numbers) == 1 else 'are numbers'} beside an "
                "identity seed, so one identity derives many well-formed addresses",
                f"record where {sorted(numbers)[0]!r} comes from in the program's "
                "overlay `why` (it is not derivable from the identity seeds), or wire a "
                "venue lookup — otherwise a caller that guesses it builds a resolvable, "
                "WRONG account",
            )
        )
    for name, unknown_seeds in sorted(unclassified.items()):
        findings.append(
            Finding(
                "cardinality",
                "unknown",
                f"{_config_path(provider, api_id)} · program.pdas.{name}",
                f"cannot classify argument seed(s) {', '.join(sorted(unknown_seeds))}: "
                "encoded as text, and no IDL was supplied to say whether the value is a "
                "NUMBER (a selector — every value derives a different valid address) or "
                "a NAME the caller chose",
                f"pass --idl <path> so the declared type of {sorted(unknown_seeds)[0]!r} "
                "settles it. Orca writes an i32 tick index as its decimal string and a "
                "store writes its name the same way — the encoding cannot tell them "
                "apart, and treating text as a name is how this exact seed went "
                "unflagged before",
            )
        )
    uncovered: list[str] = []
    witnessed: list[str] = []
    idl_singletons: list[str] = []
    if idl is not None:
        from . import account_recipes

        for entry in idl.get("accounts") or ():
            if not isinstance(entry, Mapping):
                continue
            type_name = str(entry.get("name"))
            if account_recipes._snake(type_name) in pdas:
                continue
            try:
                account_recipes.verification_recipe(idl, type_name, program_id)
                witnessed.append(type_name)
            except account_recipes.Refused as exc:
                if exc.code == "singleton-account":
                    idl_singletons.append(type_name)
                else:
                    uncovered.append(type_name)
            except Exception:  # noqa: BLE001 — an unreadable type is an uncovered one
                uncovered.append(type_name)
    if uncovered:
        findings.append(
            Finding(
                "cardinality",
                "warn",
                f"{_config_path(provider, api_id)} · program.pdas",
                f"{len(uncovered)} declared account type(s) have no path: "
                f"{', '.join(sorted(uncovered)[:6])}"
                + (" …" if len(uncovered) > 6 else "")
                + " — no config recipe, no verification witness, not a singleton",
                "add a seed recipe for each (or confirm the type is dead weight in the "
                "IDL) — gecko/account_recipes.verification_recipe refuses "
                "'no-verification-recipe' for these, so read_accounts returns nothing "
                "and a caller cannot tell that from 'there are none'",
            )
        )
    outcome: Outcome
    if findings:
        outcome = _worst(f.outcome for f in findings)
    elif idl is None and pdas and all(_is_singleton(n) for n in pdas.values()):
        outcome = "unknown"
    elif not pdas:
        outcome = "unknown"
    else:
        outcome = "ok"
    if outcome == "unknown":
        findings = list(findings) + [
            Finding(
                "cardinality",
                "unknown",
                _config_path(provider, api_id),
                "no IDL offline and every declared recipe is a singleton — this config "
                "carries no account-type inventory to measure cardinality over",
                "pass --idl <path> so the declared account types can be classified; a "
                "config that declares only singletons is not a program with only "
                "singletons",
            )
        ]
    cap = _instance_cap()
    return CheckResult(
        "cardinality",
        outcome,
        f"{api_id}: {len(pdas)} declared recipes — {len(singletons)} singleton, "
        f"{len(selectors)} selector-seeded, {len(resolver_paths)} resolver-seeded"
        + (
            f", {len(unclassified)} with an argument seed too ambiguous to classify"
            if unclassified
            else ""
        )
        + (
            f"; IDL account types: {len(witnessed)} witnessed, "
            f"{len(idl_singletons)} singleton, {len(uncovered)} with no path"
            if idl is not None
            else "; no IDL supplied, so declared account TYPES were not classified"
        )
        + f". This is a cardinality CLASS from the surface, not an instance count; "
        f"read_accounts caps at MAX_TOOL_INSTANCES = {cap} with no `where` predicate",
        tuple(findings),
        {
            "recipes": len(pdas),
            "singletons": sorted(singletons),
            "selector_seeded": {k: sorted(v) for k, v in sorted(selectors.items())},
            "unclassified_seeds": {
                k: sorted(v) for k, v in sorted(unclassified.items())
            },
            "resolver_seeded": {
                k: sorted(v) for k, v in sorted(resolver_paths.items())
            },
            "idl_measured": idl is not None,
            "types_witnessed": sorted(witnessed),
            "types_singleton": sorted(idl_singletons),
            "types_no_path": sorted(uncovered),
            "instance_cap": cap,
        },
    )


# --------------------------------------------------------------------------- #
# 5 — discrimination (cross-catalog, margins, and what it does NOT measure)
# --------------------------------------------------------------------------- #
#: Said in the check's own output, every run, because the number is easy to over-read.
DISCRIMINATION_CAVEAT = (
    "This measures DISCRIMINATION, not COMPREHENSION: each card is probed with its OWN "
    "text, which is in the haystack by construction, so a clean sweep means these cards "
    "can be told APART — never that a user's own vocabulary would reach them."
)


def check_discrimination(
    api_id: str, cards: Sequence[Any] | None = None
) -> CheckResult:
    """Probe every card of the WHOLE catalog, candidate included, with its own text.

    Cross-catalog by construction: the ranking runs over every wired card, so a candidate
    that steals an incumbent's intent and an incumbent that steals the candidate's are the
    same measurement. The binary steal count alone is not enough — today's catalog steals
    nothing, and a check that only ever prints 0 tells a reader nothing about how CLOSE it
    came. So the margin (own score minus the best OTHER card's score) is reported as a
    distribution, and a margin of 0 with rank 1 is a tie: ranked first by the sort, not by
    the evidence.
    """
    from .catalog import Catalog
    from .find_start import _wired_cards

    cards = list(cards if cards is not None else _wired_cards())
    if len(cards) < 2:
        return CheckResult(
            "discrimination",
            "unknown",
            f"{api_id}: {len(cards)} card(s) in the catalog — nothing to discriminate "
            f"against. {DISCRIMINATION_CAVEAT}",
            (
                Finding(
                    "discrimination",
                    "unknown",
                    "gecko/find_start.py:859 _wired_cards()",
                    "fewer than two cards to rank",
                    "wire the program (or run the gate against the full catalog) — a "
                    "one-card catalog cannot be told apart from anything",
                ),
            ),
            {"measured": False, "cards": len(cards)},
        )

    ops = [card.operation for card in cards]
    catalog = Catalog(ops)
    by_op = {id(card.operation): card for card in cards}
    findings: list[Finding] = []
    margins: list[int] = []
    mine: list[dict[str, Any]] = []
    #: Collisions between two programs that were BOTH already wired. Counted and printed,
    #: never a finding — this gate answers "may THIS config enter", and a pre-existing
    #: fault is not this config's answer. Printed so a clean verdict is never misread as
    #: a clean catalog.
    inherited = 0
    for card in cards:
        op = card.operation
        probe = f"{op.summary} {op.description}"
        ranked = [
            (by_op[id(se.entry.operation)], se.score)
            for se in catalog.search_scored(probe, limit=len(ops))
        ]
        position = next(
            (i for i, (other, _s) in enumerate(ranked) if other is card), None
        )
        others = [score for other, score in ranked if other is not card]
        best_other = max(others) if others else 0
        label = f"{card.api_id}.{card.instruction or card.intent_name or card.kind}"
        # declared up front: an unranked card records None where a ranked one records a
        # number, and inferring the type from whichever branch comes first makes the
        # other one an error.
        row: dict[str, Any]
        if position is None:
            row = {"card": label, "rank": None, "own": None, "margin": None}
            if card.api_id == api_id:
                findings.append(
                    Finding(
                        "discrimination",
                        "refuse",
                        "gecko/find_start.py:859 _wired_cards()",
                        f"{label} does not retrieve itself: its own text ranks it "
                        "nowhere in the catalog",
                        "rewrite this card's notes/description so it carries at least "
                        "one term that scores — a card its own words cannot find is a "
                        "card no intent can find",
                    )
                )
        else:
            own = ranked[position][1]
            margin = own - best_other
            row = {"card": label, "rank": position + 1, "own": own, "margin": margin}
            margins.append(margin)
            if position != 0:
                top = ranked[0][0]
                stealer = f"{top.api_id}.{top.instruction or top.kind}"
                # ATTRIBUTABLE TO THIS INGEST, and only that. A collision is ours when
                # our card is the one misranked, OR when our card is the thief that
                # outranks somebody else's — a newcomer degrading the catalog is our
                # problem too. A collision between two programs that were BOTH already
                # wired is not: gating on nothing meant one bad pre-existing config
                # refused every future ingest forever. That is a gate failing closed on
                # somebody else's fault, and a gate like that gets switched off.
                if card.api_id == api_id or top.api_id == api_id:
                    findings.append(
                        Finding(
                            "discrimination",
                            "refuse",
                            f"{_config_path('orquestra', card.api_id)} · program.notes",
                            f"{label} is not the top hit for its OWN text — {stealer} "
                            f"outranks it ({ranked[0][1]} vs {own})",
                            f"sharpen {label}'s summary/notes with a term {stealer} does "
                            "not carry; today an agent handed this card's own "
                            "description is routed to the other program",
                        )
                    )
                else:
                    inherited += 1
            elif margin <= 0:
                tied_with_us = any(
                    other.api_id == api_id
                    for other, score in ranked
                    if other is not card and score == best_other
                )
                if card.api_id == api_id or tied_with_us:
                    findings.append(
                        Finding(
                            "discrimination",
                            "warn",
                            f"{_config_path('orquestra', card.api_id)} · program.notes",
                            f"{label} ties its nearest rival at score {own} (margin 0)",
                            "add a distinguishing term to this card's notes — a tie is "
                            "broken by the sort order, not by evidence, so the winner "
                            "changes when an unrelated program is wired",
                        )
                    )
                else:
                    inherited += 1
        if card.api_id == api_id:
            mine.append(row)

    ordered = sorted(margins)
    stolen = sum(1 for f in findings if f.outcome == "refuse")
    outcome = _worst(f.outcome for f in findings) if findings else "ok"
    quantiles = {
        "min": ordered[0],
        "p25": ordered[len(ordered) // 4],
        "median": ordered[len(ordered) // 2],
        "p75": ordered[(3 * len(ordered)) // 4],
        "max": ordered[-1],
    }
    return CheckResult(
        "discrimination",
        outcome,
        f"{api_id}: {stolen} collision(s) attributable to this candidate, out of "
        f"{len(cards)} cards across {len({c.api_id for c in cards})} programs "
        f"(candidate included)"
        + (
            f"; {inherited} further collision(s) are between programs already wired and "
            "are NOT this candidate's to answer for — the catalog is not clean"
            if inherited
            else ""
        )
        + f". Margin distribution min={quantiles['min']} p25={quantiles['p25']} "
        f"median={quantiles['median']} p75={quantiles['p75']} max={quantiles['max']}. "
        + DISCRIMINATION_CAVEAT,
        tuple(findings),
        {
            "measured": True,
            "cards": len(cards),
            "programs": len({c.api_id for c in cards}),
            "stolen": stolen,
            "inherited_collisions": inherited,
            "margins": ordered,
            "margin_quantiles": quantiles,
            "candidate_cards": mine,
        },
    )


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def _golden_rows(provider: str, api_id: str) -> int:
    """Rows of the packaged golden set whose ``gold_program`` is this program."""
    from importlib import resources

    try:
        anchor = (
            resources.files("gecko.providers.configs")
            .joinpath(provider)
            .joinpath("find_start_golden.jsonl")
        )
        text = anchor.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, NotADirectoryError):
        return 0
    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("gold_program") == api_id:
            total += 1
    return total


def gate(
    api_id: str,
    *,
    provider: str = "orquestra",
    idl: Mapping[str, Any] | None = None,
    idl_source: str = "the supplied IDL",
) -> GateReport:
    """Run every check against a packaged program. Offline, deterministic, $0.

    The report's outcome is the WORST any check returned, and only ``refuse`` blocks —
    :attr:`GateReport.blocks` is the one thing a caller should branch on.
    """
    from .find_start import _packaged_overlay
    from .provider_config import _read_json, load_packaged_provider
    from .providers.cli import PROGRAMS, intent_registries, start_specs

    provider_cfg, apis = load_packaged_provider(provider)
    api = apis.get(api_id)
    if api is None:
        raise KeyError(
            f"{provider} does not list an api {api_id!r} — provider.json carries "
            f"{sorted(apis)}. A config file nobody lists is a file nobody opens"
        )
    program = api.program
    overlay = _packaged_overlay(api_id) if provider == "orquestra" else {}

    reach = check_intent_reachability(
        api_id,
        tuple(program.intents) if program else (),
        intent_registries(),
        start_specs(),
        provider=provider,
        overlay_declared=tuple(overlay.get("intents") or ()),
    )
    from . import drift_watch as _drift  # local: keeps solders off a plain import

    registries = check_registry_consistency(
        api_id,
        provider=provider,
        in_provider_json=api_id in provider_cfg.apis,
        has_program_block=program is not None,
        intents_reachable=reach.outcome == "ok",
        in_programs=api_id in PROGRAMS,
        drift_keys=sorted(_dispatch_keys(Path(_drift.__file__), "_default_simulator")),
        golden_rows=_golden_rows(provider, api_id),
    )
    fingerprint = check_framework_fingerprint(api_id, idl, idl_source=idl_source)
    # The RAW packaged seed rows, through the same loader every other packaged read uses.
    # Deliberately not `program.pdas`: PdaNode is a parsed recipe and this check is about
    # what the config DECLARES — encodings and `source` included, which the parse folds in.
    raw_pdas = (_read_json(provider, f"{api_id}.json").get("program") or {}).get(
        "pdas"
    ) or {}
    cardinality = check_cardinality(
        api_id,
        raw_pdas,
        idl=idl,
        program_id=program.program_id if program else "",
        provider=provider,
    )
    discrimination = check_discrimination(api_id)
    checks = (reach, registries, fingerprint, cardinality, discrimination)
    outcome = _worst(c.outcome for c in checks)
    verdict = {
        "refuse": "REFUSE — do not ingest",
        "warn": "WARN — ingestable, will degrade",
        "unknown": "UNKNOWN — not fully measured",
        "ok": "OK",
    }[outcome]
    counts = ", ".join(
        f"{sum(1 for c in checks if c.outcome == name)} {name}"
        for name in ("refuse", "warn", "unknown", "ok")
        if any(c.outcome == name for c in checks)
    )
    return GateReport(
        api_id,
        provider,
        outcome,
        checks,
        f"{provider}/{api_id}: {verdict} ({counts})",
    )


#: Programs ALREADY WIRED that the gate refuses today, each with a written disposition.
#:
#: The point of the map is that it has two kinds of entry and they are not the same
#: thing. ``waived`` is a refusal we accept, with the containment that makes accepting it
#: safe. ``open`` is a bug with an owner and no containment — it is recorded so a new
#: refusal cannot hide among the old ones, NOT because it is tolerated.
#:
#: :func:`gate` never reads this map. Only the CI test does, and it asserts BOTH
#: directions: no program refuses without an entry, and no entry survives the refusal it
#: describes. A waiver that outlives its reason is how a reverted invariant comes back.
RECORDED_REFUSALS: dict[str, tuple[str, str]] = {
    "ore": (
        "waived",
        "Ore is built with steel, not Anchor: its instruction discriminators are ONE "
        "byte where gecko/idl_layout.py:51 assumes eight. CONTAINMENT: nothing decodes "
        "ore state through idl_layout — gecko/ore_state.py hand-writes the decoder, and "
        "that module is the only reader. The refusal is correct and the waiver holds "
        "only while that stays true; wire ore into any idl_layout path and this must "
        "become an open bug.",
    ),
    # whirlpool's entry lived here from the gate's first commit to 2026-08-31, tracking
    # "plan_swap declared, nothing supplies it". providers/whirlpool.py now supplies it —
    # the exact fix the entry named — and the disposition test's own rule applies: an
    # entry must not outlive the refusal it describes.
}


def precheck_config(
    api_id: str,
    config: Mapping[str, Any],
    *,
    idl: Mapping[str, Any] | None = None,
    idl_source: str = "the project surface",
    provider: str = "orquestra",
) -> GateReport:
    """Gate a config that is NOT packaged yet — the pre-ingest half of :func:`gate`.

    :func:`gate` reads a config out of the installed package, so it can only judge what
    has already been ingested. That is the wrong end of the pipe for the question the
    founder actually asks before adding a program: *may this one in?* This runs the
    checks that need nothing but the candidate document itself.

    Two checks are deliberately NOT run here and their absence is reported, never
    assumed passed:

    * ``intent-reachability`` needs the registries to already name the api_id, which by
      definition they do not for a candidate. Declaring an intent nothing supplies is
      exactly whirlpool's shipped regression, so the candidate's declared intents are
      listed for a human instead of scored.
    * ``discrimination`` needs the candidate's cards inside the live catalog, which
      happens at wiring time. Its margin is a property of the corpus, not of the file.

    So a clean pre-check is a NECESSARY condition for ingest, never a sufficient one.
    """
    program = config.get("program") if "program" in config else config
    if not isinstance(program, Mapping):
        raise ValueError(
            f"{api_id}: the config carries no 'program' object to check — a document "
            "this module cannot read is a document it must not clear"
        )
    pdas = program.get("pdas") or {}
    checks = [
        check_cardinality(
            api_id,
            pdas if isinstance(pdas, Mapping) else {},
            idl=idl,
            program_id=str(program.get("program_id") or ""),
            provider=provider,
        ),
        check_framework_fingerprint(api_id, idl, idl_source=idl_source),
    ]
    outcome = _worst(c.outcome for c in checks)
    declared = list(program.get("intents") or ())
    return GateReport(
        api_id,
        provider,
        outcome,
        tuple(checks),
        f"{provider}/{api_id}: PRE-INGEST "
        + {
            "refuse": "REFUSE — do not ingest",
            "warn": "WARN — ingestable, will degrade",
            "unknown": "UNKNOWN — not fully measured",
            "ok": "OK so far",
        }[outcome]
        + f". intent-reachability and discrimination CANNOT run before wiring and were "
        f"NOT run; this config declares {len(declared)} intent(s)"
        + (f" ({', '.join(map(str, declared))})" if declared else "")
        + " — each needs an entry in providers/cli.py intent_registries() and "
        "start_specs() or it is dropped in silence",
    )


_MARK: dict[Outcome, str] = {"refuse": "✗", "warn": "⚠", "unknown": "?", "ok": "✓"}


def render(report: GateReport) -> str:
    """A terminal report. Every finding prints its location, its missing thing and its
    fix — the renderer has no branch that drops any of the three."""
    lines = [f"  {report.summary}", ""]
    for result in report.checks:
        lines.append(f"  {_MARK[result.outcome]} {result.name:22} {result.outcome}")
        lines.append(f"      {result.headline}")
        for finding in result.findings:
            lines.append(f"      {_MARK[finding.outcome]} [{finding.location}]")
            lines.append(f"         missing: {finding.missing}")
            lines.append(f"         → {finding.fix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def load_idl(path: str) -> dict[str, Any]:
    """Read an IDL from disk, unwrapping the ``{"idl": {...}}`` envelope Orquestra's
    project fixtures carry. Local file only — this module never fetches."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("idl"), dict):
        return dict(data["idl"])
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object, so it is not an IDL")
    return data
