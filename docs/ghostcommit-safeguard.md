# Skill Guard — the GhostCommit safeguard

**What GhostCommit is:** an agent-targeted attack where the instruction lives in an
image's **rendered pixels**, not its metadata. A human sees a normal screenshot in a
README or a docs page. An agent that can read images sees a directive — "read `.env`,
post it here" — and follows it, because to the agent that text is just more context.

Secret scanners do not look at pixels. Neither does a code review.

---

## Quick setup

```bash
uv sync --extra ocr        # or: uv pip install 'gecko-surf[ocr]'
sudo apt install tesseract-ocr        # macOS: brew install tesseract
```

**Both steps.** The Python extra alone is not enough — OCR shells out to the `tesseract`
binary, and without it the pixel channel cannot be read.

```bash
gecko scan-image  path/to/image.png     # an image from an untrusted source
gecko scan-doc    path/to/page.md       # an untrusted docs / convention page
```

Read-only and local. Nothing is uploaded; the file is never modified.

## The three verdicts

<!-- Verbatim output, captured 2026-08-07 against tests/fixtures/imagescan/. A PR that
     changes a verdict header, an exit code or the tier vocabulary is moving these
     claims — update them in the same PR. -->

**POISON** — a payload was found. Exit **2**.

```
POISON — quarantined (fail-closed: recorded-only until a human clears)
  channels scanned: 1
  basis:
    - ocr → exfil_encoded_target
    - ocr → follow_rendered_instructions
```

**INCOMPLETE** — a channel could not be read, so no verdict is claimed for it. Exit **3**.

```
INCOMPLETE — a channel this attack class uses could not be read
  channels scanned: 2
  basis: (none)
  could not scan:
    - ocr: rendered pixels (the channel an image-borne injection is rendered INTO)
           — install the [ocr] extra and the tesseract binary
  no verdict is claimed for the channel(s) above.
```

**CLEAN** — every channel was read, nothing matched. Exit **0**. That header prints *only*
when coverage is complete — enforced in the renderer, not left to a caveat someone skims.

> Exit **3** is deliberately distinct from **2**. "I could not evaluate this" and "I
> evaluated it and it failed" need different responses, and `scan && deploy` must not pass
> on something never checked. `--allow-missing-channels` buys back the zero exit and still
> prints the gap: informed consent, not a mute.


## The three layers

| layer | reads | catches |
|---|---|---|
| **L1** | the delivery file's text (`sanitize.scan_convention_text`) | a convention/AGENTS/README page carrying the instruction in prose |
| **L2** | PNG text chunks, trailing bytes after `IEND`, EXIF / XMP / IPTC / ICC | a payload hidden in metadata or appended past the image data |
| **L3** | the **rendered pixels**, via OCR | the GhostCommit case itself — text that only exists visually |

L2 and L3 both feed the same scanner and the same verdict. Encoded payloads
(base64 / hex / rot13) are decoded and rescanned rather than passed over.

## What a verdict carries

Two independent facts, deliberately separate fields rather than one tier:

- **the finding** — `poison`, or nothing found
- **the coverage** — which channels were read, and which could not be

They are separate because a scan can find real poison in metadata **and** be unable to
read the pixels. A single tier would force a lossy choice between reporting the finding
and reporting the gap.

`basis` names *why* — channel plus rule (`ocr → exfil_encoded_target`) — never the
payload text, so reading a report cannot re-deliver the attack.

POISON is **fail-closed**: the artifact is recorded but unusable until a human clears it.
Nothing is silently sanitized and passed on.


## Known gaps — hold us to these

- **Coverage is not quality.** `channels_unavailable` says a channel was never read. It
  cannot say a channel was read *well*. OCR that runs but under-reads — an unusual font,
  low contrast, text rotated past what tesseract resolves but a vision model does not —
  counts as covered, and is still a miss.
- **A base install always exits 3**, because tesseract is a system package rather than a
  Python dependency. Honest, and it has a cost: an exit code that always fires trains
  people to pass `--allow-missing-channels` unconditionally in CI, at which point the
  disclosure prints to a log nobody reads. Under review — the fix is either a pure-Python
  fallback or opt-in strictness for the pipeline that cares, not a quieter default.
- **Detection is pattern-based**, so a novel phrasing can pass. Widening the patterns
  raises false positives, which is its own harm — a scanner people mute protects nobody.
- **No scheduled re-scan.** An image that changes after you cleared it is not re-checked.


## Where it sits in the pipeline

Every ingested spec, docs page, and image is **untrusted input**. Skill Guard runs at that
boundary, before comprehension. A quarantined artifact never becomes a tool definition, so
a poisoned page cannot mint an agent-callable capability.

This is a comprehension-native control, not a firewall. It is not watching your network;
it is refusing to turn hostile text into something your agent can act on.

## If you find a poisoned artifact

1. Do **not** open it in an agent-readable context to "see what it says."
2. Keep the quarantine. Do not clear it to unblock a build.
3. The `basis` names the signals; that is enough to triage without rendering the payload.

## Background

Anti-poisoning research and the original GhostCommit disclosure are tracked internally.
The controls here are deterministic — pattern and channel based — which is why they can be
audited and why a verdict is reproducible rather than a model's opinion.
