# Demo kit — the house style for Gecko video demos

Every Gecko demo video follows one pattern, so they read as a family:
a **screenplay** (scripted pacing, real execution) → an **asciinema cast**
→ a **branded MP4** rendered by `render_cast.py`.

Reference output: `gecko_demo_full.mp4` / the TxLINE without/with demo.

## The pipeline

```bash
# 1. write the screenplay (see the voice rules below)
#    demo/kit/screenplay.py has the helpers: out/put/clear + palette

# 2. record at EXACTLY 80x20 (the renderer's cell grid assumes it)
asciinema rec --cols 80 --rows 20 -c "python3 my_screenplay.py" my_demo.cast

# 3. leak-check the cast BEFORE rendering (no secret may appear, ever)
#    grep the cast for every credential value you hold; abort on any hit.

# 4. render to the branded MP4 (+ thumbnail)
uv run --with pyte --with pillow python demo/kit/render_cast.py my_demo.cast out.mp4 \
    --scene "Gecko — scene one title|footer tagline one" \
    --scene "Gecko — scene two title|footer tagline two"
```

Scene titles/taglines advance on every clear-screen (`ESC[2J`) in the cast
after t=1s — `clear()` in the screenplay is the scene cut.

## The style contract (format)

- **1200×676 @ 30fps**, dark terminal (`#0D1117`) in a macOS-style window on a
  light frame (`#F4F7FB`); footer brand line left, scene tagline right.
- **80 columns × 20 rows.** Lines longer than 80 cols wrap ugly — keep the
  discipline in the screenplay.
- Palette (via `screenplay.py`): CYAN `$` commands, GREEN ✓ results, RED ✗
  failures, YELLOW the twist/lesson line, BOLD white headlines.
- Typewriter pacing for what a human "types" (`out`), instant for what a
  machine answers (`put`). ~45–70s total; end on the positioning line
  (`ANY API, AGENT-READY — FIRST CALL CORRECT` + `npx @geckovision/gecko`).

## The voice (narrative beats)

1. **Scene 1 — the pain, raw.** The real API, the real failures, in order
   (404 no spec → 401 → 403 with a *valid* token → 200 after the trap is
   named). Let the failure codes land; the YELLOW line names the lesson.
2. **Scene 2 — the a-ha.** `User:` states one sentence of intent → Gecko
   returns the tool/CHAIN (show the `why:` provenance line) → it runs LIVE →
   ✓ ✓ first try. Close with the positioning line.

## The honesty rules (non-negotiable)

- **Every call is real.** Status codes, counts, and names on screen are what
  the wire returned during the recording. No mocked output in a "live" demo.
- **No secrets, ever.** Tokens are read from the keychain/session inside the
  script and displayed only as `$VAR` names. Leak-check the dry run AND the
  cast file before rendering.
- **One unedited take.** Never hand-edit a `.cast`, never trim a real network
  wait (pacing lives in the screenplay's sleeps, decided *before* recording).
- **No over-claim.** Measured numbers only ("72,731 odds records"), never
  "guaranteed"; the closing line is the positioning line, not a promise.
