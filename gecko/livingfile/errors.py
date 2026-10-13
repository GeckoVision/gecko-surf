"""Typed refusals of the living file. All are ``ListJobError``s, so the lists surface
returns them as ``{"error", "code"}`` and every message passes through ``redact``."""

from __future__ import annotations

from ..orthogonal.errors import ListJobError


class UnknownMintError(ListJobError):
    code = "unknown_mint"


class LivingFileConfigError(ListJobError):
    code = "living_file_config"
