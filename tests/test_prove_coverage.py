"""Everything we built to prove a landing must be REACHABLE from the prover.

The repo's recurring bug is not broken code, it is working code nobody can reach — five
instances by 2026-09-10, the fifth being the retrieval harness itself. `prove.py` carried
the dispatch table as a local inside its dispatcher, so a new venue could be comprehended,
wired, given a landing orchestrator, and still be unprovable, with nothing to say so until
someone tried it.

This is the cheap half of the reachability gate: anything built must be enumerated, and
anything hidden on purpose must say so where a test can read it.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import gecko.providers
from gecko.prove import landing_table

#: Landings deliberately NOT dispatchable, each with the reason. Empty is the honest
#: default: a name parked here is a claim that someone decided, not that someone forgot.
HIDDEN_ON_PURPOSE: dict[str, str] = {}


def _landings_on_disk() -> dict[str, str]:
    """Every `simulate_*_landing` that exists -> the module it lives in."""
    found: dict[str, str] = {}
    for info in pkgutil.iter_modules(gecko.providers.__path__):
        if not info.name.endswith("_landing"):
            continue
        module = importlib.import_module(f"gecko.providers.{info.name}")
        for attr in dir(module):
            if re.fullmatch(r"simulate_\w+_landing", attr):
                found[attr] = info.name
    return found


def test_every_landing_orchestrator_is_reachable_from_the_prover() -> None:
    built = _landings_on_disk()
    assert built, "no landing orchestrators found — the discovery itself broke"

    reachable = {fn.__name__ for fn in landing_table().values()}
    unreachable = {
        name: mod
        for name, mod in built.items()
        if name not in reachable and name not in HIDDEN_ON_PURPOSE
    }
    assert not unreachable, (
        "built but unreachable — these landing orchestrators exist and `prove.py` "
        f"cannot dispatch to any of them: {unreachable}. Add them to `landing_table()`, "
        "or record the reason in HIDDEN_ON_PURPOSE. Hidden on purpose passes; hidden by "
        "omission fails."
    )


def test_the_prover_dispatches_to_nothing_that_vanished() -> None:
    """The other direction: a table entry whose orchestrator was renamed or deleted."""
    built = set(_landings_on_disk())
    dangling = {
        key: fn.__name__
        for key, fn in landing_table().items()
        if fn.__name__ not in built
    }
    assert not dangling, (
        f"the table points at orchestrators that no longer exist: {dangling}"
    )


def test_hidden_entries_must_carry_a_reason() -> None:
    """A gate you can silence with an empty string is not a gate."""
    for name, why in HIDDEN_ON_PURPOSE.items():
        assert why.strip(), f"{name} is excluded with no reason given"
