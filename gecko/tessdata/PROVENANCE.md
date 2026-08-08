# Vendored OCR language data — provenance and licence

This directory contains one third-party binary blob. It is vendored deliberately, and
this file is the record of what it is, where it came from, and how to verify it.

## The file

| | |
|---|---|
| File | `eng.traineddata` |
| Size | 4,113,088 bytes (3.92 MiB) |
| SHA-256 | `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2` |
| Upstream | https://github.com/tesseract-ocr/tessdata_fast |
| Path in upstream | `eng.traineddata` |
| Tag | `4.1.0` |
| Direct URL | https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0/eng.traineddata |
| Licence | Apache License 2.0 |
| Copyright | Google Inc. / the Tesseract OCR contributors |

Upstream ships the licence at
https://github.com/tesseract-ocr/tessdata_fast/blob/4.1.0/LICENSE (Apache-2.0), which
is the same licence this repository uses, so vendoring adds no new obligation beyond
attribution — which is what this file provides.

## Verify it

```bash
sha256sum gecko/tessdata/eng.traineddata
# 7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2

# Re-derive from upstream and confirm the bytes match:
curl -sSL https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0/eng.traineddata \
  | sha256sum
```

`tests/test_ocr_engine.py::test_vendored_traineddata_matches_recorded_hash` asserts
this hash on every test run.

## Why vendor it at all

The `tesserocr` manylinux/macOS wheels bundle `libtesseract` and `leptonica`, so the
OCR *engine* needs no system install. They bundle **no language data**. Measured in a
clean container with no tesseract of any kind:

- `pip install tesserocr` alone → engine loads, but has nothing to recognise with.
- `tesserocr.get_languages()` with no `TESSDATA_PREFIX` **hangs** (>10 min at 99% CPU)
  rather than failing — which is why `gecko/imagescan.py` always passes an explicit
  tessdata path and never relies on an ambient environment variable.

So without this file, `pip install 'gecko-surf[ocr]'` is not a complete install and the
pixel channel silently stays unreadable. Vendoring is what makes the extra honest.

## Why `tessdata_fast` and not `tessdata` / `tessdata_best`

Measured sizes for `eng.traineddata` at tag `4.1.0`:

| Variant | Size | Note |
|---|---|---|
| `tessdata_fast` | 3.92 MiB | **chosen** |
| `tessdata_best` | 14.69 MiB | |
| `tessdata` | 22.38 MiB | legacy + LSTM combined |

The decision was made on measured recall, not on size alone: `tessdata_fast` produces
**basis-identical results on all 28 committed image fixtures** versus the previous
engine (system tesseract 5.3.4 with Debian's own `eng.traineddata`). Since the
detection basis — not the recovered text — is what drives the verdict, quarantine and
exit code, identical basis means the swap is security-neutral. Paying 4x the bytes for
`tessdata_best` would buy no additional detection on any attack we can currently
demonstrate.

## Updating it

Changing this file changes what the scanner can read, so treat it as a
security-relevant change:

1. Replace the blob and update the size + SHA-256 above.
2. Re-run `uv run pytest tests/test_ocr_recall_corpus.py tests/test_ocr_engine.py
   tests/test_ocrnorm.py`.
3. The recall corpus must still measure 13/18 with the same five named residuals and
   zero false positives. A change in those numbers is the finding — record it, do not
   adjust the expectations to match.
4. That 13/18 is the engine **plus** `gecko.ocrnorm`, and the two must not be conflated
   when judging an engine swap. Recall here is a product of what the engine reads AND of
   undoing the renderer's line breaks and shattered base58 runs; a new engine that wraps
   or spaces text differently moves the number without reading the pixels any better.
   `tests/test_ocrnorm.py` isolates the normalisation half and needs no engine at all.
