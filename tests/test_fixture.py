import json
from pathlib import Path

def test_fixture_has_cross_checkable_oom_evidence():
    data=json.loads((Path(__file__).parents[1]/"backend/fixtures/docker_state.json").read_text())
    assert data["inspect"]["api"]["oom_killed"] is True
    assert data["inspect"]["api"]["exit_code"] == 137
    assert data["stats"]["api"]["memory_percent"] >= 95

