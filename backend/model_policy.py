"""Role selection without changing the generic fast/quality models."""
from contextvars import ContextVar

MODEL_ROLE = ContextVar("docker_ops_model_role", default="fast")


def select_model(settings, quality=False):
    role = "quality" if quality else MODEL_ROLE.get()
    fast, strong = settings.models
    router = (settings.openai_router_model if settings.backend == "openai" else settings.router_model) or fast
    return (strong if quality else router if role == "router" else fast), role
