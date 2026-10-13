"""Supplier watch through Orthogonal: who a supplier is, who owns it, this month's news,
trust signals, an SDN name check and website changes — each field from a first source
and, where there is one, a second, with agreement computed in code.

Self-maintaining: a field whose first source drifts (``drift.py``) or fails moves to
the next source in ``sources.SOURCE_MAP`` (``repair.py``), is checked against the last
good snapshot, and is published or kept stale. The stored runs answer ``latest``,
``history``, ``changes_since`` and ``sources`` for free (``dataset.py``).

Reuses the commenter job's core unchanged in kind: the keyring session, the allowlist,
the cents guard, the ``parentRequestId`` paginator and the typed errors. Entry points:
``plan_watch`` / ``run_watch`` and the ``plan_supplier_watch`` / ``run_supplier_watch``
tools on ``gecko serve --lists --stdio``.
"""

from .deps import SupplierDeps, supplier_deps_for
from .job import WatchResult, plan_watch, run_watch
from .plan import WatchPlan
from .store import read_runs

__all__ = [
    "SupplierDeps",
    "WatchPlan",
    "WatchResult",
    "plan_watch",
    "read_runs",
    "run_watch",
    "supplier_deps_for",
]
