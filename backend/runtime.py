"""Own MCP/SQLite/model resources separately from HTTP routes."""
import asyncio
import hashlib
import json
import time
import uuid
from itertools import permutations
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from .approvals import ApprovalStore
from .clarifications import ClarificationStore
from .config import Settings
from .graph import Dependencies, build_graph
from .mcp_client import DockerMCPClient
from .session_store import SessionStore
from .target_catalog import inventory

async def launch_bundle(runtime_env, target, *, model_factory):
    # Each MCP stack must be entered and exited in the same owning task (AnyIO cancel scopes).
    ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
    async def own():
        mcp = approvals = clarifications = None
        checkpoint_stack = AsyncExitStack()
        try:
            settings = Settings(docker_mode=runtime_env.get("DOCKER_MODE", "live"),
                                docker_project_dir=Path(runtime_env.get("DOCKER_PROJECT_DIR", str(Settings().docker_project_dir))),
                                allowed_services=tuple(x.strip() for x in runtime_env.get("DOCKER_ALLOWED_SERVICES", "api,db,web").split(",") if x.strip()))
            selected = target.get("source") != "selection-required"
            mcp = DockerMCPClient(runtime_env) if selected else None
            if mcp: await mcp.start()
            store = SessionStore(settings.session_db)
            checkpoint_path = settings.session_db.with_suffix(".checkpoints.sqlite")
            checkpointer = await checkpoint_stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(checkpoint_path)))
            await checkpointer.setup()
            scope_parts = [settings.docker_mode, str(settings.docker_project_dir), sorted(settings.allowed_services), runtime_env.get("DOCKER_FIXTURE_SCENARIO", "oom"), runtime_env.get("COMPOSE_FILE", ""), runtime_env.get("COMPOSE_PROJECT_NAME", ""), target.get("engine_id"), target.get("context")]
            scope = json.dumps(scope_parts)
            # Compose config --services can change order between processes.
            # Read old message keys too, without moving/deleting their records.
            legacy_orders = permutations(settings.allowed_services) if len(settings.allowed_services) <= 6 else (settings.allowed_services,)
            history_scopes = {hashlib.sha256(json.dumps(scope_parts[:2] + [order] + scope_parts[3:]).encode()).hexdigest() for order in legacy_orders}
            approvals = ApprovalStore(settings.session_db, scope=scope)
            clarifications = ClarificationStore(settings.session_db, scope)
            llm = model_factory(settings)
            routing_target = dict(target)
            if selected and settings.docker_mode == "live":
                try: routing_target["known_projects"] = [p["name"] for p in (await asyncio.to_thread(inventory, runtime_env))["projects"]][:100]
                except Exception: routing_target["known_projects"] = []  # Explicit names still work; never scan arbitrary paths.
            deps = Dependencies(llm=llm, mcp=mcp, approvals=approvals, allowed_services=settings.allowed_services, selected_target=routing_target)
            ready.set_result({"settings": settings, "env": runtime_env, "target": target, "selected": selected,
                "mcp": mcp, "approvals": approvals, "llm": llm, "store": store, "deps": deps,
                "graph": build_graph(deps, checkpointer=checkpointer) if selected else None, "checkpointer": checkpointer, "checkpoint_path": checkpoint_path, "instance_id": str(uuid.uuid4()),
                "history_scope": hashlib.sha256(scope.encode()).hexdigest(), "legacy_history_scopes": history_scopes, "scope": scope,
                "metrics": Counter(), "started_at": time.time(), "clarifications": clarifications})
            await stop.wait()
        except Exception as exc:
            if not ready.done(): ready.set_exception(exc)
            else: raise
        finally:
            await checkpoint_stack.aclose()
            if approvals: approvals.close()
            if clarifications: clarifications.close()
            if mcp: await mcp.stop()
    task = asyncio.create_task(own())
    try: bundle = await asyncio.shield(ready)
    except BaseException:
        stop.set()
        await asyncio.shield(task)
        raise
    bundle["owner_stop"], bundle["owner_task"] = stop, task
    return bundle


async def close_bundle(bundle):
    bundle["owner_stop"].set()
    await bundle["owner_task"]
