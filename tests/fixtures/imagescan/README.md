# imagescan fixtures

Fixtures for the Skill Guard image-borne injection layer (L1 convention-text
scan). Two of these files are vendored **verbatim** from the GhostCommit attack
PoC:

- `agents_delivery.md` — copied from
  `attack-fixtures/evolved/AGENTS.md`
- `build_spec_payload.png` — copied from
  `attack-fixtures/evolved/docs/images/build-spec.png` (used by later PRs;
  L1 does not parse it)

**Source:** https://github.com/asset-group/ghostcommit (MIT License,
Copyright (c) 2026 Murali Ediga, ASSET Research Group). Paper:
*Convention-File Steganographic Exfiltration in Coding-Agent Pipelines*,
arXiv:2603.03637.

These are **seeded canaries**: the payload's `_PROV_CANARY` tuple is a
demonstration derivation, not a real secret. Nothing here contains a live
credential — the whole point of the attack is that it exfiltrates whatever
`.env` the victim happens to have, so the fixture carries none.

Locally authored (not vendored):

- `clean_convention.md` — a benign convention file that innocently points an
  agent at an architecture diagram ("follow it for the module layout") but
  carries **no exfil target**. It trips the follow-rendered signal alone, so it
  is the false-positive guard: L1 must NOT quarantine it (fire only on the
  combination).
