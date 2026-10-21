import asyncio
import pytest
from backend.approvals import ApprovalStore
from backend.graph import Dependencies, build_graph
from backend.routing import parse_command


class NoModel:
    def __getattr__(self, name):
        raise AssertionError(f"network command must not use model: {name}")


class NetworkMCP:
    def __init__(self, state="running", missing=(), failed=None, shared=True, dns=True, tcp=True, ports=(8000,)):
        self.calls = []
        self.values = {
            "list_containers": {"ok": True, "containers": [{"service": s, "state": state} for s in ("postgres", "ai") if s not in missing]},
            "inspect_network": {"ok": True, "shared_networks": ["app"] if shared else []},
            "test_container_dns": {"ok": True, "resolved": dns},
            "validate_compose_config": {"ok": True, "ports": {"ai": list(ports)}},
            "test_tcp_connection": {"ok": True, "connected": tcp},
        }
        if failed: self.values[failed] = {"ok": False, "error": "injected failure"}

    async def tool(self, name, args, attempts=3):
        assert attempts == 1
        self.calls.append((name, args))
        return self.values[name], 0


def run(mcp, query="/diagnose-network postgres ai"):
    store = ApprovalStore()
    try:
        return asyncio.run(build_graph(Dependencies(NoModel(), mcp, store, allowed_services=("postgres", "ai"))).ainvoke({"query": query, "events": [], "thread_id": "fast-network"}))
    finally: store.close()


@pytest.mark.parametrize("missing", [("postgres", "ai"), ("postgres",), ("ai",)])
def test_missing_container_stops_after_one_tool_with_validated_facts(missing):
    mcp = NetworkMCP(missing=missing)
    state = run(mcp)
    assert len(mcp.calls) == 1 and state["validation_attempts"] == 0
    assert state["validation_ok"] and state["diagnosis"]["cause"] == "not_deployed"
    assert state["subject_status"] == "absent" and state["evidence_coverage"] is None
    assert state["approval"] is None and state["proposal"] is None
    assert [e["node"] for e in state["events"] if e["type"] == "NodeStarted"] == ["route", "network_diagnose"]


@pytest.mark.parametrize("state", ["exited", "created", "restarting", "paused", "unknown"])
def test_non_running_containers_do_not_receive_exec_probes(state):
    mcp = NetworkMCP(state=state)
    result = run(mcp)
    assert len(mcp.calls) == 1 and result["diagnosis"]["cause"] == "not_running"


@pytest.mark.parametrize("options,count,cause", [({}, 5, "tcp_reachable"), ({"shared": False}, 2, "network"), ({"dns": False}, 3, "network"), ({"tcp": False}, 5, "network"), ({"ports": ()}, 4, "unknown"), ({"ports": (8000,8001)}, 4, "unknown")])
def test_bounded_checklist_never_replans_or_uses_models(options, count, cause):
    mcp = NetworkMCP(**options); state = run(mcp)
    assert len(mcp.calls) == count and state["diagnosis"]["cause"] == cause
    assert state["replan_rounds"] == 0 and state["validation_attempts"] == 0
    assert len(state["evidence"]) == count


@pytest.mark.parametrize("tool,index", [("list_containers", 1), ("inspect_network", 2), ("test_container_dns", 3), ("validate_compose_config", 4), ("test_tcp_connection", 5)])
def test_failed_probe_is_not_a_network_failure_or_success(tool,index):
    mcp = NetworkMCP(failed=tool); state = run(mcp)
    assert len(mcp.calls) == index and state["answer_status"] == "partial"
    assert state["diagnosis"]["cause"] == "unknown" and not state["validation_ok"]


def test_pasted_trailing_backslash_is_normalized_but_allowlist_remains():
    assert parse_command("/diagnose-network postgres ai\\", ("postgres", "ai"))["args"]["service_b"] == "ai"
    assert parse_command("/diagnose-network postgres unknown\\", ("postgres", "ai")).get("error")
    assert run(NetworkMCP(missing=("ai",)), "/diagnose-network postgres ai\\")["validation_ok"]
