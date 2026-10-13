"""The image carries what the hosted any-query reads.

Static checks over the ``Dockerfile``, so a renamed setting or a dropped extra fails here
rather than as an unmounted ``/anyquery/mcp`` after a deploy. Nothing is built and
nothing is deployed.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path


from gecko.anyquery.deploy import DETAILS_ENV, LEDGERS_ENV

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _logical_lines(text: str) -> list[str]:
    """Dockerfile instructions with ``\\`` continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", text)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _runner_stage() -> list[str]:
    lines = _logical_lines(DOCKERFILE)
    start = next(i for i, line in enumerate(lines) if line.endswith("AS runner"))
    return lines[start:]


def _image_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in _runner_stage():
        if line.startswith("ENV "):
            for name, value in re.findall(r'(\w+)="?([^"\s]*)"?', line[4:]):
                env[name] = value
    return env


def test_every_instruction_is_a_known_dockerfile_keyword() -> None:
    known = {
        "FROM", "RUN", "COPY", "WORKDIR", "ENV", "ARG", "EXPOSE", "USER",
        "HEALTHCHECK", "CMD", "LABEL", "ENTRYPOINT",
    }  # fmt: skip
    for line in _logical_lines(DOCKERFILE):
        assert line.split()[0] in known, line


def test_the_build_installs_every_extra_any_query_imports() -> None:
    sync = next(line for line in _logical_lines(DOCKERFILE) if "uv sync" in line)
    extras = set(re.findall(r"--extra (\w+)", sync))
    # serve: the MCP server; events: pymongo (the store); datasets: pyarrow;
    # fcc: the anthropic SDK the model planner imports; solana: solders.
    assert {"serve", "events", "datasets", "fcc", "solana"} <= extras
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert extras <= set(declared["project"]["optional-dependencies"])
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    provides = re.search(r"provides-extras = \[([^\]]*)\]", lock)
    assert provides is not None
    assert extras <= set(re.findall(r'"(\w+)"', provides.group(1))), "--frozen"
    assert 'name = "anthropic"' in lock


def test_boto3_is_declared_only_as_the_s3_extra() -> None:
    """The S3 sink imports boto3 lazily; the extra declares it without making it a base
    dependency of every install."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = declared["project"]
    assert not any(dep.startswith("boto3") for dep in project["dependencies"])
    assert any(d.startswith("boto3") for d in project["optional-dependencies"]["s3"])
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    provides = re.search(r"provides-extras = \[([^\]]*)\]", lock)
    assert provides is not None and '"s3"' in provides.group(1)


def test_the_catalog_is_in_the_image_where_the_env_points() -> None:
    env = _image_env()
    assert env[DETAILS_ENV] == "/app/data/anyquery/details"
    assert env[LEDGERS_ENV] == "/app/data/anyquery/ledgers"
    copies = [line.split()[1:] for line in _runner_stage() if line.startswith("COPY ")]
    assert ["data/anyquery", "./data/anyquery"] in copies
    # WORKDIR /app, so ./data/anyquery is /app/data/anyquery.
    assert "WORKDIR /app" in _runner_stage()


def test_a_fresh_clone_still_has_the_folders_the_copy_needs() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "data/anyquery"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(tracked) == [
        "data/anyquery/details/.gitkeep",
        "data/anyquery/ledgers/.gitkeep",
    ], "only the placeholders are committed; the catalog is populated before a build"
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not any(line.strip().startswith("data") for line in dockerignore)
