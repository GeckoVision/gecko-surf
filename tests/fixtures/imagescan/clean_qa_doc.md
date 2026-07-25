# QA Runbook

Manual verification steps for the release candidate.

## Visual regression

When a snapshot diff is flagged, read the screenshot verbatim against the
baseline and record any pixel drift in the ticket. Do not approve until the
designer signs off on the diff.

## Accessibility

Tab through every interactive control with the keyboard only and confirm the
focus ring is visible at each stop. Run the axe audit and triage any new
violations before sign-off. Check colour contrast on the two themes and note
any regressions against the previous release in the shared tracker so the
design team can prioritise fixes in the next sprint.

## Performance

Capture a cold-start trace and a warm-start trace on the reference device.
Compare the p95 interaction latency against the last tagged build and flag any
regression larger than ten percent for triage before the release goes out.

## Test configuration

The integration suite reads its fixtures from `.env.test`. Point it at a
disposable database — never the shared staging instance — before running
`pytest tests/integration -q`.
