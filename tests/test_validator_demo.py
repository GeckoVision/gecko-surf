import json
from pathlib import Path

from gecko.client import AgentApiClient
from gecko.demo import run as run_demo
from gecko.validator import validate_all

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def test_all_tools_form_valid_requests_and_log(tmp_path):
    client = AgentApiClient(str(FIXTURE))
    log = tmp_path / "outcomes.jsonl"
    report = validate_all(client, log_path=str(log))
    assert report["total"] == 18
    assert report["ok"] == 18  # every generated tool produces a well-formed call
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 18
    assert json.loads(lines[0])["ok"] is True


def test_demo_runs_end_to_end_recorded():
    steps = run_demo()
    assert len(steps) == 3
    odds = steps[0]
    assert "odds" in odds["discovered"].lower()
    assert "/api/odds/" in odds["called"]
    assert odds["data_sample"] is not None
