import asyncio
import json

import httpx
import pytest

from backend.config import Settings
from backend.llm import ModelGateway
from backend import usage


def test_routing_schema_is_sent_to_ollama_and_failed_attempt_is_counted(monkeypatch):
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        raise httpx.ReadTimeout("injected")
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client_type(*a, **kw, transport=httpx.MockTransport(handle)))
    gateway = ModelGateway(Settings(backend="ollama"))
    async def run():
        ledger = usage.begin()
        with pytest.raises(httpx.ReadTimeout):
            await gateway.structured_json("classify", schema={"type": "object", "required": ["route"]}, system="route")
        return ledger
    ledger = asyncio.run(run())
    assert requests[0]["format"]["required"] == ["route"]
    assert requests[0]["options"]["num_predict"] == 512
    assert len(ledger["calls"]) == 1 and ledger["calls"][0]["kind"] == "error"
    assert usage.summarize(ledger)["total_tokens"] is None
    assert usage.summarize({"calls": []})["total_tokens"] == 0


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_role_prompt_and_fast_model_reach_actual_provider_transport(monkeypatch, backend):
    requests = []
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if backend == "ollama":
            return httpx.Response(200, json={"message": {"content": '{"ok":true}'}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client_type(*a, **kw, transport=httpx.MockTransport(handle)))
    monkeypatch.setenv("OPENAI_API_KEY", "transport-test-only")
    gateway = ModelGateway(Settings(backend=backend))
    assert asyncio.run(gateway.json("test", system="runtime role")) == {"ok": True}
    assert requests[0]["model"] == gateway.fast
    assert requests[0]["messages"][0]["content"] == "runtime role"
    if backend == "ollama": assert requests[0]["think"] is False


@pytest.mark.parametrize("backend", ["ollama", "openai"])
def test_real_provider_stream_parser_preserves_role_and_model(monkeypatch, backend):
    requests = []
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if backend == "ollama":
            content = '\n'.join(json.dumps({"message": {"content": x}}) for x in ("hello", " world"))
        else:
            content = '\n'.join("data: " + json.dumps({"choices": [{"delta": {"content": x}}]}) for x in ("hello", " world")) + '\ndata: [DONE]\n'
        return httpx.Response(200, text=content)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client_type(*a, **kw, transport=httpx.MockTransport(handle)))
    monkeypatch.setenv("OPENAI_API_KEY", "transport-test-only")
    gateway = ModelGateway(Settings(backend=backend))
    async def consume():
        return "".join([chunk async for chunk in gateway.stream_text("test", quality=False, system="chat role")])
    assert asyncio.run(consume()) == "hello world"
    assert requests[0]["model"] == gateway.fast
    assert requests[0]["messages"][0]["content"] == "chat role"
