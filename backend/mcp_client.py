from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from .execution_policy import CURRENT_BUDGET
from .observability import span
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


class DockerMCPClient:
    def __init__(self, env: dict | None = None):
        self.env = dict(os.environ if env is None else env)
        self.stack: AsyncExitStack | None = None
        self.sessions: dict[str, ClientSession] = {}
        self.approval_secret = secrets.token_urlsafe(32)
        self.read_slots = asyncio.Semaphore(int(self.env.get("READ_TOOL_CONCURRENCY", "4")))

    @property
    def session(self) -> ClientSession | None:
        return self.sessions.get("ops")

    async def start(self) -> None:
        self.stack = AsyncExitStack()
        for name, script in {"docs": ROOT / "mcp_docs_server.py", "ops": ROOT / "mcp_server.py"}.items():
            env = {**self.env, "FASTMCP_LOG_LEVEL": "ERROR"}
            if name == "ops":
                env["MCP_APPROVAL_SECRET"] = self.approval_secret
            params = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)
            read, write = await self.stack.enter_async_context(stdio_client(params))
            session = await self.stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session

    async def stop(self) -> None:
        if self.stack: await self.stack.aclose()

    async def tool(self, name: str, args: dict, attempts: int = 3) -> tuple[dict, int]:
        if name in {"restart_service", "apply_project_config_patch", "deploy_project_config", "execute_runtime_action"}:
            with span("mcp." + name, "tool"):
                return await self._tool(name, args, attempts=1)
        async with self.read_slots:
            with span("mcp." + name, "tool"):
                return await self._tool(name, args, attempts)

    async def _tool(self, name: str, args: dict, attempts: int = 3) -> tuple[dict, int]:
        assert self.session
        if name in {"restart_service", "apply_project_config_patch", "deploy_project_config", "execute_runtime_action"}:
            attempts = 1  # A timeout cannot prove whether a mutation already ran.
        for n in range(attempts):
            try:
                budget = CURRENT_BUDGET.get()
                if budget: budget.reserve("tool")
                result = await self.session.call_tool(name, args)
                text = result.content[0].text
                if getattr(result, "isError", getattr(result, "is_error", False)):
                    raise ValueError(f"MCP tool {name} rejected arguments: {text}")
                structured = getattr(result, "structuredContent", getattr(result, "structured_content", None))
                if isinstance(structured, dict) and structured:
                    return structured.get("result", structured), n
                try: return json.loads(text), n
                except json.JSONDecodeError: return {"raw": text}, n
            except asyncio.CancelledError as exc:
                exc.retries = n
                raise
            except (asyncio.TimeoutError, ConnectionError) as e:
                if n + 1 == attempts:
                    e.retries = n
                    raise e
                await asyncio.sleep(0.2 * 2**n)
        raise RuntimeError("unreachable")

    async def resource(self, uri: str) -> str:
        for session in self.sessions.values():
            listed = await session.list_resource_templates()
            templates = getattr(listed, "resourceTemplates", getattr(listed, "resource_templates", []))
            if any(str(getattr(x, "uriTemplate", getattr(x, "uri_template", ""))).startswith("docker://") for x in templates):
                result = await session.read_resource(uri)
                return result.contents[0].text
        raise ValueError(f"MCP resource provider not found: {uri}")

    async def named_prompt(self, name: str, arguments: dict[str, str]) -> str:
        for session in self.sessions.values():
            listed = await session.list_prompts()
            if any(x.name == name for x in listed.prompts):
                result = await session.get_prompt(name, arguments)
                return result.messages[0].content.text
        raise ValueError(f"MCP prompt provider not found: {name}")

    async def prompt(self, symptom: str) -> str:
        return await self.named_prompt("docker_incident_prompt", {"symptom": symptom})
