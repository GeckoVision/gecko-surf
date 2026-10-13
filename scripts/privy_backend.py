"""Moved to :mod:`gecko.privy_backend` (any-query billing, 2026-10-08). Re-exported here so
every script and test that imports ``scripts.privy_backend`` keeps working unchanged."""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.privy_backend import *  # noqa: E402,F403
from gecko.privy_backend import (  # noqa: E402,F401
    _compact_u16,
    _default_transport,
    _canonical,
)
