"""Safe correlation and a narrow Phase 3 OpenTelemetry extension point."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

request_id: ContextVar[str] = ContextVar("request_id", default="background")
run_id: ContextVar[str] = ContextVar("run_id", default="-")
logger = logging.getLogger("opspilot")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("level=%(levelname)s %(message)s"))
    logger.addHandler(handler)


@contextmanager
def span(name: str) -> Iterator[None]:
    """Replace the internals with tracer.start_as_current_span in Phase 3.

    Fixed operation names only. Never record exception objects or payloads.
    Context nesting already follows request -> retrieval -> embedding and LLM, and
    request -> agent.run -> agent.llm / agent.tool -> gitlab.request; agent.approval.
    """
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        logger.info(
            "operation=%s request_id=%s run_id=%s outcome=%s duration_ms=%.2f",
            name,
            request_id.get(),
            run_id.get(),
            outcome,
            (time.monotonic() - started) * 1000,
        )
