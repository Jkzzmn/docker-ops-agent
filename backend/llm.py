from __future__ import annotations

import json
import asyncio
import os
import time
from typing import Any, AsyncIterator

import httpx

from . import usage
from .config import Settings
from .model_policy import select_model, MODEL_ROLE
from .execution_policy import CURRENT_BUDGET, BudgetExceeded


SYSTEM = """You are a cautious senior Docker operations engineer. Use only supplied evidence.
Separate observed facts from hypotheses. Cite evidence IDs like [E1]. Never invent commands or state.
Mutating recovery actions require explicit human approval. Reply in Korean."""


class ModelGateway:
    def __init__(self, settings: Settings):
        self.s = settings
        self.fast, self.quality = settings.models
        self._slots = asyncio.Semaphore(settings.model_concurrency)

    async def _limited(self, operation):
        budget = CURRENT_BUDGET.get()
        async with asyncio.timeout(budget.remaining() if budget else 120):
            async with self._slots:
                if budget: budget.reserve("model")
                return await operation()

    async def route_json(self, prompt, *, schema, system):
        try: context = {"routing_input": json.loads(prompt.split("Input=", 1)[1])}
        except (ValueError, IndexError): context = {}
        token = MODEL_ROLE.set("router")
        if select_model(self.s)[0] == self.fast: context = {}
        try:
            try:
                value = await self.structured_json(prompt, schema=schema.model_json_schema(), system=system)
                return schema.model_validate(value, context=context).model_dump()
            except (ValueError, httpx.TransportError, TimeoutError):
                if select_model(self.s)[0] == self.fast: raise
                MODEL_ROLE.set("fast")
                ledger = usage.CURRENT_USAGE.get()
                if ledger is not None: ledger["router_escalations"] = ledger.get("router_escalations", 0) + 1
                value = await self.structured_json(prompt, schema=schema.model_json_schema(), system=system)
                return schema.model_validate(value).model_dump()
        finally: MODEL_ROLE.reset(token)

    async def _ask(self, prompt: str, *, quality: bool = False, json_mode: bool = False, system: str = SYSTEM, schema: dict | None = None) -> str:
        started = time.perf_counter()
        try:
            return await self._limited(lambda: self._ask_impl(prompt, quality=quality, json_mode=json_mode, system=system, schema=schema))
        except BudgetExceeded:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            # Failed requests consume time too; never report them as zero model calls.
            model, role = select_model(self.s, quality)
            usage.record("error", model, system, prompt, type(exc).__name__, None, None, (time.perf_counter()-started)*1000, quality, role)
            raise

    async def _ask_impl(self, prompt: str, *, quality: bool = False, json_mode: bool = False, system: str = SYSTEM, schema: dict | None = None) -> str:
        model, role = select_model(self.s, quality)
        started = time.perf_counter()
        if self.s.backend == "ollama":
            payload: dict[str, Any] = {"model": model, "stream": False, "think": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                                       "options": {"num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))}}
            if json_mode:
                payload["format"] = schema or "json"
                if schema: payload["options"].update(temperature=0, num_predict=512)
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"{self.s.ollama_url}/api/chat", json=payload)
                r.raise_for_status()
                body = r.json()
            text, tokens = body["message"]["content"], (body.get("prompt_eval_count"), body.get("eval_count"))
        else:
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
            payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                r.raise_for_status()
                body = r.json()
            text, used = body["choices"][0]["message"]["content"], body.get("usage", {})
            tokens = (used.get("prompt_tokens"), used.get("completion_tokens"))
        usage.record("json" if json_mode else "text", model, system, prompt, text, *tokens, (time.perf_counter() - started) * 1000, quality, role)
        return text

    async def structured_json(self, prompt: str, *, schema: dict, system: str = SYSTEM, quality: bool = False) -> dict:
        # Ollama constrains decoding with JSON Schema; callers also validate locally.
        # OpenAI retains JSON mode here; the same local validation fails closed.
        raw = await self._ask(prompt, json_mode=True, system=system, schema=schema, quality=quality)
        return json.loads(raw)

    async def json(self, prompt: str, *, quality: bool = False, system: str = SYSTEM) -> dict[str, Any]:
        raw = await self._ask(prompt, quality=quality, json_mode=True, system=system)
        try:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            if isinstance(parsed, dict): return parsed
            if isinstance(parsed, list):
                if all(isinstance(item, dict) and "tool" in item for item in parsed):
                    return {"tasks": parsed}
                return parsed[0] if len(parsed) == 1 and isinstance(parsed[0], dict) else {}
            return {}
        except Exception:
            return {}

    async def text(self, prompt: str, *, quality: bool = True, system: str = SYSTEM) -> str:
        return await self._ask(prompt, quality=quality, system=system)

    async def stream_text(self, prompt: str, *, quality: bool = True, system: str = SYSTEM) -> AsyncIterator[str]:
        budget = CURRENT_BUDGET.get()
        async with asyncio.timeout(budget.remaining() if budget else 240):
            async with self._slots:
                if budget: budget.reserve("model")
                async for delta in self._stream_text(prompt, quality=quality, system=system): yield delta

    async def _stream_text(self, prompt: str, *, quality: bool = True, system: str = SYSTEM) -> AsyncIterator[str]:
        model, role = select_model(self.s, quality)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        started, chunks, tokens = time.perf_counter(), [], (None, None)
        try:
            if self.s.backend == "ollama":
                async with httpx.AsyncClient(timeout=240) as client:
                    async with client.stream("POST", f"{self.s.ollama_url}/api/chat", json={"model": model, "stream": True, "think": False, "messages": messages, "options": {"num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192"))}}) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line: continue
                            part = json.loads(line)
                            if part.get("done"):
                                tokens = (part.get("prompt_eval_count"), part.get("eval_count"))
                            delta = part.get("message", {}).get("content", "")
                            if delta:
                                chunks.append(delta)
                                yield delta
                return
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
            body = {"model": model, "stream": True, "stream_options": {"include_usage": True}, "messages": messages}
            async with httpx.AsyncClient(timeout=240) as client:
                async with client.stream("POST", "https://api.openai.com/v1/chat/completions", headers=headers, json=body) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]": continue
                        part = json.loads(line[6:])
                        if part.get("usage"):
                            tokens = (part["usage"].get("prompt_tokens"), part["usage"].get("completion_tokens"))
                        choices = part.get("choices") or [{}]
                        delta = choices[0].get("delta", {}).get("content", "")
                        if delta:
                            chunks.append(delta)
                            yield delta
        finally:
            usage.record("stream", model, system, prompt, "".join(chunks), *tokens, (time.perf_counter() - started) * 1000, quality, role)
