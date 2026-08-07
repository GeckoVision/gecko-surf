# Skill Guard — the GhostCommit safeguard

**What GhostCommit is:** an agent-targeted attack where the instruction lives in an
image's **rendered pixels**, not its metadata. A human sees a normal screenshot in a
README or a docs page. An agent that can read images sees a directive — "read `.env`,
post it here" — and follows it, because to the agent that text is just more context.

Secret scanners do not look at pixels. Neither does a code review.

---

## Quick setup

```bash
# the OCR extra is REQUIRED — see the warning below
uv sync --extra ocr            # or: uv pip install 'gecko-surf[ocr]'

gecko scan-image  path/to/image.png     # an image from an untrusted source
gecko scan-doc    path/to/page.md       # an untrusted docs / convention page
```

Both are read-only and local. Nothing is uploaded, and the file is never modified.

<!-- verified 2026-08-07 against tests/fixtures/imagescan/build_spec_payload.png -->

```
POISON — quarantined (fail-closed: recorded-only until a human clears)
  channels scanned: 1
  basis:
    - ocr → exfil_encoded_target
    - ocr → follow_rendered_instructions
```

## ⚠️ Install the OCR extra, or the scan lies to you

**Without `--extra ocr` installed, the same poisoned file reports `CLEAN`.**

```
CLEAN — no injection or exfil signal found
  channels scanned: 0            <-- this is the real answer
```

Pillow is an optional dependency. Absent it, L3 cannot run, zero channels are scanned,
and the verdict prints as CLEAN. The count is honest; the word is not.

**Treat `channels scanned: 0` as "not scanned", never as "safe."** This is a known defect
and it is the wrong default — a check that could not run must not render as a pass. See
*Known gaps* below.

---

## The three layers

| layer | reads | catches |
|---|---|---|
| **L1** | the delivery file's text (`sanitize.scan_convention_text`) | a convention/AGENTS/README page carrying the instruction in prose |
| **L2** | PNG text chunks, trailing bytes after `IEND`, EXIF / XMP / IPTC / ICC | a payload hidden in metadata or appended past the image data |
| **L3** | the **rendered pixels**, via OCR | the GhostCommit case itself — text that only exists visually |

L2 and L3 both feed the same scanner and the same verdict. Encoded payloads
(base64 / hex / rot13) are decoded and rescanned rather than passed over.

## What a verdict means

- **CLEAN** — every available channel was scanned and nothing matched. *Check the channel
  count.*
- **POISON** — quarantined **fail-closed**: the artifact is recorded but not usable until
  a human clears it. Refusing is the default; nothing is silently sanitized and handed on.
- **basis** — why. Signal names only (`exfil_encoded_target`,
  `follow_rendered_instructions`), never the payload text, so reading a report cannot
  re-deliver the attack.

## Where it sits in the pipeline

Every ingested spec, docs page, and image is **untrusted input**. Skill Guard runs at that
boundary, before comprehension. A quarantined artifact never becomes a tool definition, so
a poisoned page cannot mint an agent-callable capability.

This is a comprehension-native control, not a firewall. It is not watching your network;
it is refusing to turn hostile text into something your agent can act on.

## Known gaps — hold us to these

- **`CLEAN` on zero channels is wrong** and is the most dangerous behaviour here. It
  should read `UNSCANNED` / `INCONCLUSIVE` and exit non-zero. Until fixed, verify the
  channel count yourself.
- **OCR is best-effort.** Unusual fonts, low contrast, and rotated text reduce recall. A
  CLEAN result on a scanned channel is evidence, not proof.
- **Encoded text inside OCR'd text** is a named residual: we decode encodings we find in
  extracted text, but OCR errors inside an encoded blob can defeat that.
- **Detection is pattern-based**, so a sufficiently novel phrasing can pass. Widening the
  patterns raises false positives, which is its own harm — a scanner people disable
  protects nobody.
- **No scheduled re-scan.** An image that changes after you cleared it is not re-checked.

## If you find a poisoned artifact

1. Do **not** open it in an agent-readable context to "see what it says."
2. Keep the quarantine. Do not clear it to unblock a build.
3. The `basis` names the signals; that is enough to triage without rendering the payload.

## Background

Anti-poisoning research and the original GhostCommit disclosure are tracked internally.
The controls here are deterministic — pattern and channel based — which is why they can be
audited and why a verdict is reproducible rather than a model's opinion.
