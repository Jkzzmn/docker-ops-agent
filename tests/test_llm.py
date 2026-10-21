import asyncio

from backend.config import Settings
from backend.llm import ModelGateway


def test_json_handles_task_arrays_and_fenced_objects():
    class Stub(ModelGateway):
        raw = ""
        async def _ask(self, *args, **kwargs):
            return self.raw
    gateway = Stub(Settings(backend="ollama"))
    gateway.raw = '[{"tool":"list_containers","args":{}},{"tool":"get_docker_engine_status","args":{}}]'
    assert len(asyncio.run(gateway.json("plan"))["tasks"]) == 2
    gateway.raw = '```json\n{"ok": true}\n```'
    assert asyncio.run(gateway.json("validate")) == {"ok": True}
    gateway.raw = 'not valid JSON'
    assert asyncio.run(gateway.json("plan")) == {}
