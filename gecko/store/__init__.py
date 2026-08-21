"""The Mongo serving/aggregate layer — control-plane only, by construction.

This package is the SERVING layer for the hosted flow (catalog, attributed
outcomes, per-endpoint scores). It is NOT a second capture sink: the
append-only allowlist in :mod:`gecko.corpus` stays the write-once, structurally
payload-free substrate, and NOTHING here may widen it.

The boundary, stated once and enforced in code (architecture §2):

* :mod:`gecko.store.projections` is the ONLY writer into the outcome collection.
  It calls :func:`gecko.corpus.to_record` / ``assert_allowlisted`` FIRST, then
  attaches ``run_id`` + ``api_key_id`` + the derived ``source`` — there is no
  raw-insert path, so the store cannot hold a response payload, a param value, a
  secret, or a wallet↔tx binding.
* :mod:`gecko.store.scores` is the ONLY reader for PUBLISHED numbers, and it
  reads the observed-only view — so an ad-hoc query can never leak a
  playground/synthetic row into a provider's scorecard.

Collections are duck-typed (:mod:`gecko.store.collections`): tests run entirely
in-memory, production binds the same protocol to Mongo. No hard pymongo import
lives in the engine path.
"""

from __future__ import annotations

from .catalog_sync import (
    Endpoint,
    ProgramSurface,
    let_me_buy_surface,
    surface_spec_rev,
    sync_surface,
)
from .collections import Collection, InMemoryCollection
from .projections import (
    ProjectionError,
    record_call_outcome,
    record_simulated_outcome,
)
from .scores import N_FLOOR, EndpointScore, endpoint_score

__all__ = [
    "Collection",
    "InMemoryCollection",
    "ProjectionError",
    "record_call_outcome",
    "record_simulated_outcome",
    "N_FLOOR",
    "EndpointScore",
    "endpoint_score",
    "Endpoint",
    "ProgramSurface",
    "let_me_buy_surface",
    "surface_spec_rev",
    "sync_surface",
]
