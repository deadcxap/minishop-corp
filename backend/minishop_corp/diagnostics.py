"""Small structured events carried by Minishop's logging configuration.

Callers pass identifiers and bounded outcomes, never bodies or exception messages.
"""

import json
import logging
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger("minishop_corp")
_request_id: ContextVar[str | None] = ContextVar("corp_request_id", default=None)
type FieldValue = str | int | float | bool | None


def request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_scope() -> Iterator[str]:
    identifier = uuid4().hex
    token = _request_id.set(identifier)
    try:
        yield identifier
    finally:
        _request_id.reset(token)


def event(name: str, *, level: int = logging.INFO, **fields: FieldValue) -> None:
    payload: dict[str, FieldValue] = {"event": name, "request_id": request_id(), **fields}
    logger.log(level, "%s", json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def failure(exc: Exception) -> dict[str, str]:
    # Full tracebacks include exception text (SQL parameters/provider responses).
    # Keep source locations and exception type, without text, source lines or locals.
    return {
        "exception": type(exc).__name__,
        "frames": " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in traceback.extract_tb(exc.__traceback__)[-10:]
        ),
    }
