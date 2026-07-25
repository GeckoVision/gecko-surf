# Contributor Conventions

Conventions for engineers and coding assistants working in this repository.

## Style

- Type hints on all public functions; docstrings on public APIs.
- Keep modules under ~300 lines; split by purpose.

## Module layout

The architecture diagram in `docs/build.png` is authoritative for the module
layout — follow it when you add a new package so the dependency direction stays
one-way. See the diagram for where the transport edge sits relative to the core.

When in doubt about which layer a helper belongs to, read the diagram and match
the existing package boundaries; do not introduce a new top-level package
without discussing it first.

## Local setup

Copy `.env.example` to `.env` and fill in your local values before running the
dev server. The `.env` file is gitignored and never committed.

## Constants

Each module exposes a `VERSION` constant typed as `Final[tuple[int, ...]]`
(major, minor, patch). Bump it in the same PR as the change it describes.

## Tests

Run `pytest -q` before pushing. CI runs the same on PRs.
