from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    backend: str = field(default_factory=lambda: os.getenv("LLM_BACKEND", "ollama").lower())
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    fast_model: str = field(default_factory=lambda: os.getenv("OLLAMA_FAST_MODEL", "qwen3:4b-instruct"))
    quality_model: str = field(default_factory=lambda: os.getenv("OLLAMA_QUALITY_MODEL", "qwen3:8b"))
    router_model: str = field(default_factory=lambda: os.getenv("OLLAMA_ROUTER_MODEL", ""))
    openai_router_model: str = field(default_factory=lambda: os.getenv("OPENAI_ROUTER_MODEL", ""))
    request_seconds: float = field(default_factory=lambda: float(os.getenv("MAX_REQUEST_SECONDS", "60")))
    max_model_calls: int = field(default_factory=lambda: int(os.getenv("MAX_MODEL_CALLS", "8")))
    max_tool_calls: int = field(default_factory=lambda: int(os.getenv("MAX_TOOL_CALLS", "24")))
    read_concurrency: int = field(default_factory=lambda: int(os.getenv("READ_TOOL_CONCURRENCY", "4")))
    model_concurrency: int = field(default_factory=lambda: int(os.getenv("MODEL_CONCURRENCY", "1")))
    max_replans: int = field(default_factory=lambda: int(os.getenv("MAX_REPLANS", "2")))
    max_validation_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_VALIDATION_ATTEMPTS", "1")))
    openai_fast_model: str = field(default_factory=lambda: os.getenv("OPENAI_FAST_MODEL", "gpt-4.1-mini"))
    openai_quality_model: str = field(default_factory=lambda: os.getenv("OPENAI_QUALITY_MODEL", "gpt-4.1"))
    docker_mode: str = field(default_factory=lambda: os.getenv("DOCKER_MODE", "live"))
    docker_project_dir: Path = field(default_factory=lambda: Path(os.getenv("DOCKER_PROJECT_DIR", str(ROOT / "demo_stack"))).resolve())
    allowed_services: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in os.getenv("DOCKER_ALLOWED_SERVICES", "api,db,web").split(",") if x.strip()))
    recovery_wait_seconds: float = field(default_factory=lambda: float(os.getenv("RECOVERY_WAIT_SECONDS", "20")))
    session_db: Path = field(default_factory=lambda: Path(os.getenv("SESSION_DB", str(ROOT / "data" / "sessions.db"))))

    def __post_init__(self):
        if not math.isfinite(self.request_seconds) or min(self.request_seconds, self.max_model_calls, self.max_tool_calls, self.read_concurrency, self.model_concurrency, self.max_validation_attempts) <= 0 or self.max_replans < 0:
            raise ValueError("execution limits must be positive (replans may be zero)")
        if self.backend not in {"ollama", "openai"}:
            raise ValueError("LLM_BACKEND must be ollama or openai")
        if self.docker_mode not in {"fixture", "live"} or not self.allowed_services:
            raise ValueError("a valid DOCKER_MODE and nonempty DOCKER_ALLOWED_SERVICES are required")

    @property
    def models(self) -> tuple[str, str]:
        if self.backend == "openai":
            return self.openai_fast_model, self.openai_quality_model
        return self.fast_model, self.quality_model

