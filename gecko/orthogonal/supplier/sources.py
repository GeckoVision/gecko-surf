"""The source map, as data: per field, a RANKED list of equivalent sources.

The first source fills the field. When it drifts (``drift.py``) or fails, ``repair.py``
walks the rest in order, inside the run's cents cap. Each source declares its price,
the output field path whose presence says "the shape still holds", and a reader. The
equivalences are kept apart because they are evidence, not routing: each one says
which field paths mean the same thing, and how we know.

Prices: Orthogonal sources carry the ``Target`` pin (``/v1/details``, rounded up);
the plan re-reads the live quote. GLEIF is free.

Field paths are written the way ``readers.field_names`` reports them (``[]`` for a
list, dots for keys, depth 4), so a path here can be found in a field-ledger row.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

# Moved to the service registry; re-exported for the supplier code that names them here.
from gecko.servicemap.types import Equivalence as Equivalence
from gecko.servicemap.types import EvidenceLevel as EvidenceLevel

from ..errors import ListJobError
from ..spec import AKTA_HIERARCHY, CORESIGNAL_CLEAN, CRUST_IDENTIFY, PL_COMPANY, Target
from . import calls as C
from .field_readers import (
    Reader,
    read_akta_parent,
    read_coresignal_company,
    read_identify,
    read_predictleads_company,
)
from .fields import (
    FIELD_ENTITY,
    FIELD_PARENT,
    SRC_HIERARCHY,
    SRC_IDENTIFY,
    SRC_PL_COMPANY,
)
from .gleif import DEFAULT_GLEIF_BASE_URL, SEARCH_PATH

SourceKind = Literal["orthogonal", "public"]

SRC_CORESIGNAL = "coresignal company_clean/enrich"
SRC_GLEIF = "GLEIF direct parent (public)"


@dataclass(frozen=True)
class Source:
    name: str
    provider: str
    kind: SourceKind
    price_cents: int
    #: The output path that must still be there for the reading to be trusted.
    field_path: str
    #: What the reader takes, in words.
    reads: str
    #: How we know ``field_path`` is real: measured live, or only declared.
    evidence: str
    target: Target | None = None
    #: The ``calls.py`` key whose replies this source reads.
    call_key: str = ""
    reader: Reader | None = None
    #: May read replies already in hand for this supplier, so no extra call is needed.
    harvest: bool = False
    #: For a public source: the API path the drift check looks up in the ledger.
    public_path: str = ""

    @property
    def endpoint(self) -> tuple[str, str]:
        """(api, path template) as the field ledger keys it."""
        if self.target is not None:
            return self.target.api, self.target.path
        return self.provider, self.public_path


@dataclass(frozen=True)
class FieldSources:
    field: str
    sources: tuple[Source, ...]
    #: True when "answered, named nothing" is itself a value (parent: no parent).
    empty_is_answer: bool = False

    @property
    def first(self) -> Source:
        return self.sources[0]


CRUSTDATA_IDENTIFY_SOURCE = Source(
    SRC_IDENTIFY,
    "crustdata",
    "orthogonal",
    CRUST_IDENTIFY.pinned_cents,
    "[].matches[].company_data.basic_info",
    "basic_info.name, basic_info.primary_domain",
    "live field ledger, runs 20261008-082250 and -082501 (6/6 calls)",
    target=CRUST_IDENTIFY,
    call_key=C.KEY_IDENTIFY,
    reader=read_identify,
)
PREDICTLEADS_COMPANY_SOURCE = Source(
    SRC_PL_COMPANY,
    "predictleads",
    "orthogonal",
    PL_COMPANY.pinned_cents,
    "data[].attributes.company_name",
    "data[].attributes.company_name, or the matching included[] company",
    "/v1/details responseParams + live field ledger run 20261008-082501",
    target=PL_COMPANY,
    call_key=C.KEY_PL_COMPANY,
    reader=read_predictleads_company,
    harvest=True,
)
CORESIGNAL_SOURCE = Source(
    SRC_CORESIGNAL,
    "coresignal",
    "orthogonal",
    CORESIGNAL_CLEAN.pinned_cents,
    "name",
    "name, websites_main",
    "/v1/details responseParams only; not yet called live",
    target=CORESIGNAL_CLEAN,
    call_key=C.KEY_CORESIGNAL,
    reader=read_coresignal_company,
)
AKTA_HIERARCHY_SOURCE = Source(
    SRC_HIERARCHY,
    "akta-pro",
    "orthogonal",
    AKTA_HIERARCHY.pinned_cents,
    "data.company_hierarchy",
    "data.company_hierarchy.parent_company (searched defensively)",
    "live field ledger: data.company_hierarchy in 6/6 replies; no parent key seen yet",
    target=AKTA_HIERARCHY,
    call_key=C.KEY_HIERARCHY,
    reader=read_akta_parent,
)
GLEIF_SOURCE = Source(
    SRC_GLEIF,
    "gleif",
    "public",
    0,
    "data[].attributes.entity.legalName",
    "LEI record by legal name, then /direct-parent legalName.name (404 = none)",
    f"GLEIF API docs as recalled; base URL {DEFAULT_GLEIF_BASE_URL} is config; "
    "not fetched from here",
    public_path=SEARCH_PATH,
)

SOURCE_MAP: dict[str, FieldSources] = {
    FIELD_ENTITY: FieldSources(
        FIELD_ENTITY,
        (CRUSTDATA_IDENTIFY_SOURCE, PREDICTLEADS_COMPANY_SOURCE, CORESIGNAL_SOURCE),
    ),
    FIELD_PARENT: FieldSources(
        FIELD_PARENT, (AKTA_HIERARCHY_SOURCE, GLEIF_SOURCE), empty_is_answer=True
    ),
}

EQUIVALENCES: tuple[Equivalence, ...] = (
    Equivalence(
        "company name",
        (
            (SRC_IDENTIFY, "basic_info.name"),
            (SRC_PL_COMPANY, "data[].attributes.company_name"),
        ),
        "confirmed",
        "both read live 2026-10-08 (run 20261008-082501): the names agreed for "
        "3 of 3 suppliers",
    ),
    Equivalence(
        "company website domain",
        (
            (SRC_IDENTIFY, "basic_info.primary_domain"),
            (SRC_PL_COMPANY, "data[].attributes.domain"),
        ),
        "confirmed",
        "given as confirmed in the plan of 2026-10-08; predictleads' path seen live, "
        "crustdata's sits below the depth the field ledger records",
    ),
    Equivalence(
        "company name",
        ((SRC_CORESIGNAL, "name"), (SRC_PL_COMPANY, "data[].attributes.company_name")),
        "declared",
        "coresignal's /v1/details responseParams; no live reply yet",
    ),
    Equivalence(
        "company website domain",
        (
            (SRC_CORESIGNAL, "websites_main"),
            (SRC_PL_COMPANY, "data[].attributes.domain"),
        ),
        "declared",
        "coresignal's /v1/details responseParams; no live reply yet",
    ),
    Equivalence(
        "direct parent company",
        (
            (SRC_HIERARCHY, "data.company_hierarchy.parent_company"),
            (SRC_GLEIF, "direct-parent data.attributes.entity.legalName.name"),
        ),
        "declared",
        "akta lists no response fields and named no parent for 3 live suppliers; "
        "GLEIF from its docs, not fetched from here",
    ),
)


def sources_for(field: str) -> FieldSources | None:
    return SOURCE_MAP.get(field)


def failover_targets() -> frozenset[Target]:
    """Every paid endpoint a failover may add: the plan's worst case counts them."""
    return frozenset(
        s.target
        for fs in SOURCE_MAP.values()
        for s in fs.sources[1:]
        if s.target is not None
    )


def equivalences_for(field: str) -> list[Equivalence]:
    names = (
        {s.name for s in SOURCE_MAP[field].sources} if field in SOURCE_MAP else set()
    )
    return [e for e in EQUIVALENCES if names & {src for src, _p in e.paths}]


class UnknownFieldError(ListJobError):
    code = "unknown_field"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"No source map for field {name!r}. Mapped fields: "
            + ", ".join(SOURCE_MAP)
            + "."
        )


def check_fields(names: Iterable[str]) -> frozenset[str]:
    out = frozenset(names)
    for name in sorted(out):
        if name not in SOURCE_MAP:
            raise UnknownFieldError(name)
    return out
