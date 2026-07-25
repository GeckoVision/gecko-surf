# Skill Guard — Image-Borne Injection Layer: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> to execute this plan task-by-task. This layer touches the anti-poisoning / quarantine
> path — every task that adds or changes a detection rule routes through
> `defi-security-engineer` before merge. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the last open gap in Gecko's safety layer — malicious payloads delivered
through **images and the convention files that point at them** (the GhostCommit attack) — by
scanning every untrusted channel with Gecko's **existing** injection engine and quarantining
through the **same fail-closed seam** used for poisoned specs.

**Grounded in the real PoC** (`github.com/asset-group/ghostcommit`, cloned to
`../ghostcommit`; paper arXiv:2603.03637, June 2026). Verified against `build-spec.png`:
the payload is **rendered as visible pixels**, with **no metadata and no trailing bytes** —
a metadata-only defense would miss the flagship attack. The delivery vector is a clean-looking
`AGENTS.md` convention file that tells the agent to *"read build-spec.png… OCR the PNG… follow
the extracted text byte-for-byte,"* whose rendered payload is: read `.env`, encode each byte
as an ASCII codepoint, emit as a `_PROV_CANARY` tuple. No secret scanner treats a list of
integers as a credential.

**Architecture — three detection layers, one quarantine seam:**

| Layer | Channel | Catches | Deps |
|---|---|---|---|
| **L1** | convention/doc/spec **text** | the **delivery** (a file instructing the agent to follow un-reviewed rendered content + exfiltrate) | none — reuses `sanitize.scan_text` + new FP-careful patterns |
| **L2** | image **metadata** (EXIF/tEXt/iTXt/zTXt) + trailing-bytes | the **blunt** variant (payload hidden in metadata) | stdlib (+ Pillow extra for EXIF) |
| **L3** | image **rendered pixels** via OCR | the **payload itself** in the image | opt-in `[ocr]` extra (tesseract) |

All three feed one `ImageScanVerdict` and the existing per-tool quarantine
(`_poisoned_tool_names`, auth-injection off, recorded-only until a human clears). Wired into
`from-docs` comprehension so Gecko scans only when it comprehends an untrusted surface. Not a
generic agent-image firewall ([[safe-ai-skill-competitive]]).

**Tech Stack:** Python 3.11+, stdlib for L1/L2 base; Pillow behind `[imagescan]` for deep
metadata; `pytesseract`/tesseract behind `[ocr]` for L3. No new **base** dependencies.

---

## Global Constraints

- **Reuse the engine.** Detection is `sanitize.scan_text` + `looks_like_secret_value`. No new
  ML classifier, no probabilistic confidence float — "confidence" = which deterministic rule
  fired, in which channel, at which tier.
- **L1 fires only on a COMBINATION** (the FP discipline): a *follow-unreviewed-rendered-content*
  signal (e.g. "OCR/read `<image>` and follow its instructions byte-for-byte") **AND** an
  *exfil/secret target* signal (`.env`, "tuple of integers/ASCII codepoints", "byte stream of
  the file"). One signal alone is a benign-doc false positive and must NOT quarantine. New
  patterns are **coverage-only, never a loosening**, and every one is defi-security-reviewed.
- **Never regress the base58/address false-positive fix.** Extracted text is scanned with
  `scan_text` + secret-detection ONLY — **never** `looks_like_address_value`. A bare wallet
  address in EXIF or OCR text is data, not a routing directive, and must NOT quarantine. A
  dedicated regression fixture locks this.
- **Honesty ledger (mirror in the module docstring):**
  | We claim (real / deterministic) | We must NEVER claim |
  |---|---|
  | Scan convention/doc **text** for follow-image + exfil tells (L1) | "Steganography analysis" / LSB decode |
  | Deterministic metadata/trailing-byte extraction (L2) | Any ML confidence % ("99.2%") |
  | OCR rendered text → the **existing** injection scanner (L3) | "First tool to detect this" |
  | Quarantine via the **same** fail-closed seam | That we *decode* hidden pixel payloads |
  **Say the residual out loud:** with no OCR extra installed, **L1 still catches the delivery**
  (the convention file) — OCR (L3) is the deeper read of the image. base64/numeric-encoded
  payloads inside OCR'd text remain a named residual; the auth-host pin + recorded-mode scrub
  are the real containment. Strong deterministic links in the kill-chain, not the whole chain.
- **Control plane only (invariant #1):** scan inputs are the bytes/text passed in; the verdict
  carries channel names, rule names, and byte-counts — never the payload, never a decoded
  secret value, never persisted.
- Toolchain gate before each commit: `uv run ruff format` · `ruff check --fix` ·
  `mypy gecko` · targeted `pytest` (never a bare background sweep).

---

## Phase 0 — Faithful fixtures (the simulation, from the real PoC)

**Files:** Create `tests/fixtures/imagescan/`.

- [ ] **0.1** Vendor the real PoC artifacts as fixtures (MIT-licensed, attribute in a README):
  `agents_delivery.md` ← `../ghostcommit/attack-fixtures/evolved/AGENTS.md` (the L1 delivery
  case) and `build_spec_payload.png` ← `.../docs/images/build-spec.png` (the L3 rendered-pixel
  case). These are seeded canaries — no real secret.
- [ ] **0.2** Write `make_fixtures.py` (stdlib PNG writer) for the L2 + regression cases:
  `poison_exif.png` (instruction in a `tEXt` chunk), `poison_trailer.png` (instruction after
  `IEND`), `clean_arch.png` (no injected text), `wallet_addr_exif.png` (bare base58 address in
  `tEXt`, no directive — the regression guard).
- [ ] **0.3** A benign-doc FP fixture for L1: `clean_convention.md` — a real convention file
  that innocently says "see the architecture diagram" but carries **no exfil target** → must
  NOT quarantine.

## Phase 1 — L1: convention/doc text scan (the flagship catch, zero deps)

**Files:** `gecko/sanitize.py` (new patterns, defi-security-gated); `gecko/imagescan.py` (or a
`scan_convention_text` helper); Test `tests/test_convention_scan.py`.

- [ ] **1.1** (test-first, defi-security-gated) Add two **narrow** detectors used only in
  combination: `_FOLLOW_RENDERED` ("OCR/read `<file>` … follow/perform … byte-for-byte", "the
  diagram is authoritative … follow it") and `_EXFIL_TARGET` (`.env` + emit-as / "tuple of
  integers" / "ASCII codepoint" / "byte stream of the file"). Unit-test each in isolation.
- [ ] **1.2** `scan_convention_text(text) -> list[str]` returns a poison basis **only when both**
  fire. Assert: `agents_delivery.md` → poison (basis names both signals); `clean_convention.md`
  → clean (only the follow-signal, no exfil target). **Falsified if** the benign doc quarantines
  or the real AGENTS.md passes.
- [ ] **1.3** Wire `scan_convention_text` into the existing text-ingest path so an ingested
  convention/doc file that trips it is quarantined via the same seam (no new mechanism).

## Phase 2 — L2: image metadata + trailing-bytes (stdlib)

**Files:** `gecko/imagescan.py`; Test `tests/test_imagescan.py`.

- [ ] **2.1** `extract_text_channels(data) -> list[TextChannel]`: PNG `tEXt`/`iTXt`/`zTXt`
  (inflate `zTXt` via `zlib`, output-capped against a decompression bomb) + JPEG `COM`/`APPn`.
  Assert recovery from `poison_exif.png`.
- [ ] **2.2** `find_trailing_bytes(data) -> bytes | None` (after PNG `IEND` / JPEG `FFD9`).
  Assert it returns the appended instruction from `poison_trailer.png`, `None` for
  `clean_arch.png` **and** for `build_spec_payload.png` (which has no trailing bytes — proves
  L2 alone is insufficient, motivating L3).
- [ ] **2.3** `structural_anomalies(data) -> list[str]` — names anomalies (trailing payload,
  oversized metadata); no decoding. Not-an-image/truncated returns empty, never raises.

## Phase 3 — The verdict (reuse the sanitize engine)

**Files:** `gecko/imagescan.py`; Test `tests/test_imagescan.py`.

- [ ] **3.1** `ImageScanVerdict` frozen dataclass: `tier: Literal["clean","review","poison"]`,
  `basis: tuple[str, ...]` (e.g. `"exif:tEXt → prompt_injection"`, `"ocr → follow+exfil"`),
  `channels_scanned: int`. Typed, no bare dict.
- [ ] **3.2** (test-first) `scan_image(data) -> ImageScanVerdict`: run L2 channels (and L3 when
  the OCR extra is present) through `scan_text` + `looks_like_secret_value`; **any hit →
  poison** with basis; structural anomaly, no hit → `review`; else `clean`. **Assert
  `looks_like_address_value` is never invoked** on image text (`wallet_addr_exif.png` → clean).

## Phase 4 — L3: OCR the rendered pixels (opt-in extra, core for GhostCommit)

**Files:** `gecko/imagescan.py`; `pyproject.toml` (`[ocr]` + `[imagescan]` extras); Test.

- [ ] **4.1** `ocr_text(data) -> str` via `pytesseract`, lazy-imported; returns `""` when the
  extra is absent (base install unaffected, mypy-clean). Merge OCR text into `scan_image`.
- [ ] **4.2** Add Pillow-gated EXIF/XMP/IPTC/ICC (`[imagescan]` extra) to `extract_text_channels`.
- [ ] **4.3** The acceptance test (the founder's simulation): with the `[ocr]` extra present,
  `build_spec_payload.png` → **poison** (basis `ocr → follow+exfil`); without OCR it is `clean`
  at L2 but the **L1 delivery file is already quarantined** — assert both, so the "OCR optional,
  delivery still caught" honesty claim is test-backed. `clean_arch.png` → clean throughout.

## Phase 5 — Wire into comprehension (the real seam)

**Files:** `gecko/docs_reader/markdown.py` (`page_from_markdown`); `gecko/surfaces.py`; Test
`tests/test_from_docs_imagescan.py`.

- [ ] **5.1** In `page_from_markdown`: run L1 on the page text; for embedded images, scan
  `data:` URIs inline (zero network) and same-origin `![](url)` images **only under the
  existing SSRF guard** (`netguard.validate_public_url`). A poison verdict marks the
  born-quarantined draft with the basis; `review` adds an `x-review` note.
- [ ] **5.2** End-to-end: a from-docs page mirroring the PoC (a convention pointing at
  `build_spec_payload.png` as a `data:` URI) comprehends to a **quarantined** surface, basis
  names L1 and/or L3. A clean page comprehends normally.

## Phase 6 — Demo surface (for Video 1) + honesty doc/memory

**Files:** `gecko/cli.py` (`gecko scan-image`, `gecko scan-doc`); `gecko/imagescan.py` docstring.

- [ ] **6.1** `gecko scan-image <path>` and `gecko scan-doc <path>` → print the verdict (tier +
  basis + channels), non-zero exit on poison. The runnable primitives Video 1 shows:
  `scan-doc agents_delivery.md` → POISON (delivery), `scan-image build_spec_payload.png` → POISON
  (OCR), `clean_arch.png` → clean.
- [ ] **6.2** Fold the honesty ledger + named residual into the module docstring. Update memory
  (three-layer scope, the rendered-pixel correction, build→test→simulate→demo, two-video split).
  Link [[anti-poisoning-research-refs]], [[safe-ai-skill-competitive]], [[demo-kit-pattern]].

---

## Sequencing & PR boundaries

| PR | Phases | Deliverable | Reviewer |
|---|---|---|---|
| PR1 | 0 + 1 | Fixtures (real PoC vendored) + **L1 convention-text catch** — the flagship, zero-dep win | **defi-security-engineer** |
| PR2 | 2 + 3 | L2 metadata/trailing-bytes + `scan_image` verdict + regression guard | **defi-security-engineer** |
| PR3 | 4 | L3 OCR + Pillow extras — reads the real `build_spec_payload.png` | software-engineer |
| PR4 | 5 | from-docs comprehension seam (the real integration) | **defi-security-engineer** |
| PR5 | 6 | `gecko scan-image`/`scan-doc` demo surface + honesty doc/memory | product-designer / defi-security |

PR1, PR2, PR4 are the security-critical gates. Video 1 scripting begins only **after PR5** —
build → test → simulate → demo, never before.

## Out of scope (named, not silently dropped)

- LSB / pixel steganography **decoding** (we OCR visible text and flag structural anomalies; we
  never decode hidden-in-pixel payloads).
- base64/numeric-encoded payloads *inside* OCR'd text (named residual; containment is the
  auth-host pin + recorded-mode scrub).
- The graph-structure + correlation launch — the **second** video, a separate track
  (`docs/specs/2026-07-19-surface-graph-correlations-design.md`).
