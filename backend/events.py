from langgraph.config import get_stream_writer
import time
import uuid


def emit(events: list[dict], event: dict) -> None:
    event.setdefault("event_id", uuid.uuid4().hex)
    event.setdefault("timestamp", time.time())
    events.append(event)
    try:
        get_stream_writer()(event)
    except RuntimeError:
        pass
