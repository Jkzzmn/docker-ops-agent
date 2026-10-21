import os

import pytest


os.environ["LLM_BACKEND"] = "ollama"
os.environ["DOCKER_MODE"] = "fixture"
os.environ["LANGSMITH_TRACING"] = "false"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("DOCKER_FIXTURE_SCENARIO", "oom")
    monkeypatch.setenv("RECOVERY_WAIT_SECONDS", "0.3")
    from backend import server
    from tests.model_responses import ScriptedModel
    monkeypatch.setattr(server, "ModelGateway", lambda settings: ScriptedModel())
