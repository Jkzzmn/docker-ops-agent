import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.approvals import ApprovalStore, sign_approval, verify_approval
from backend import docker_tools as tools


def test_signature_binds_tool_args_expiry_and_secret():
    token = sign_approval("secret", "one", "restart_service", {"service": "api"}, time.time() + 60)
    assert verify_approval("secret", token, "restart_service", {"service": "api"})["id"] == "one"
    for secret, tool, args in [("wrong", "restart_service", {"service": "api"}), ("secret", "restart_service", {"service": "db"}), ("secret", "shell", {"service": "api"})]:
        with pytest.raises(PermissionError):
            verify_approval(secret, token, tool, args)
    with pytest.raises(PermissionError):
        verify_approval("secret", sign_approval("secret", "two", "restart_service", {}, time.time() - 1), "restart_service", {})
    with pytest.raises(PermissionError):
        verify_approval("secret", "approved:forged", "restart_service", {"service": "api"})


def test_persisted_claim_is_atomic_and_scope_bound(tmp_path):
    path = tmp_path / "actions.db"
    store = ApprovalStore(path, "scope-a")
    action = store.create("thread", "restart_service", {"service": "api"}, "test")
    store.close()
    wrong = ApprovalStore(path, "scope-b")
    with pytest.raises(ValueError): wrong.claim(action.id, "executing")
    wrong.close()
    store = ApprovalStore(path, "scope-a")
    def claim(_):
        try:
            store.claim(action.id, "executing")
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    expired = store.create("thread", "restart_service", {}, "expired", ttl=-1)
    with pytest.raises(ValueError): store.claim(expired.id, "executing")
    store.close()


def test_mcp_rejects_forged_replayed_and_changed_tokens(monkeypatch):
    monkeypatch.setattr(tools, "MODE", "fixture")
    monkeypatch.setattr(tools, "USED_APPROVALS", {})
    monkeypatch.setattr(tools, "RECOVERED", set())
    monkeypatch.setenv("MCP_APPROVAL_SECRET", "secret")
    with pytest.raises(PermissionError): tools.restart_service("api", "approved:forged")
    token = sign_approval("secret", "once", "restart_service", {"service": "api"}, time.time() + 60)
    with pytest.raises(PermissionError): tools.restart_service("db", token)
    assert tools.restart_service("api", token)["ok"]
    with pytest.raises(PermissionError): tools.restart_service("api", token)


def test_mutation_timeout_is_never_retried():
    import asyncio
    from backend.mcp_client import DockerMCPClient
    class LostResponse:
        calls = 0
        async def call_tool(self, name, args):
            self.calls += 1
            raise ConnectionError("response lost after execution")
    session = LostResponse()
    client = DockerMCPClient()
    client.sessions["ops"] = session
    with pytest.raises(ConnectionError): asyncio.run(client.tool("restart_service", {}))
    assert session.calls == 1
