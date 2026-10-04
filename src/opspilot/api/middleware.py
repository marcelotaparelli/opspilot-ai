"""Pure ASGI correlation, tracing, request deadline and bounded body buffering."""

import asyncio
import json
import logging
import time
from uuid import uuid4

from opentelemetry.propagate import extract
from opentelemetry.trace import Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opspilot.observability import annotate, count, log_event, observe, request_id, span, trace_ids

MAX_BODY_BYTES = 512_000


class RequestMiddleware:
    def __init__(self, app: ASGIApp, timeout: float) -> None:
        self.app = app
        self.timeout = timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation = str(uuid4())
        token = request_id.set(correlation)
        started = False
        status = 500
        clock = time.monotonic()
        # W3C trace context from the caller (if any); request_id stays server-generated.
        carrier = {
            key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]
        }
        parent = extract({k: v for k, v in carrier.items() if k in ("traceparent", "tracestate")})

        async def correlated_send(message: Message) -> None:
            nonlocal started, status
            if message["type"] == "http.response.start":
                started = True
                status = int(message["status"])
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", correlation.encode()),
                    (b"x-trace-id", trace_ids()[0].encode()),
                ]
            await send(message)

        async def error_response(code_status: int, code: str) -> None:
            if not started:
                await correlated_send(
                    {
                        "type": "http.response.start",
                        "status": code_status,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await correlated_send(
                    {
                        "type": "http.response.body",
                        "body": json.dumps({"error": code, "request_id": correlation}).encode(),
                    }
                )

        try:
            with span(
                "http.request", parent=parent, http__request__method=scope["method"]
            ) as current:
                try:
                    async with asyncio.timeout(self.timeout):
                        messages: list[Message] = []
                        size = 0
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            size += len(message.get("body", b""))
                            if size > MAX_BODY_BYTES:
                                await error_response(413, "request_too_large")
                                return
                            messages.append(message)
                            if not message.get("more_body", False):
                                break
                        index = 0

                        async def bounded_receive() -> Message:
                            nonlocal index
                            if index < len(messages):
                                item = messages[index]
                                index += 1
                                return item
                            return await receive()

                        await self.app(scope, bounded_receive, correlated_send)
                except TimeoutError:
                    await error_response(504, "request_timeout")
                except Exception:
                    log_event(
                        {"operation": "http.request", "outcome": "internal_error"}, logging.ERROR
                    )
                    await error_response(500, "internal_error")
                finally:
                    route = getattr(scope.get("route"), "path", "unmatched")
                    annotate(current, http__route=route, http__response__status_code=status)
                    if status >= 500:
                        current.set_status(Status(StatusCode.ERROR))
                    labels = {"method": scope["method"], "route": route}
                    count("http_server_requests_total", status_class=f"{status // 100}xx", **labels)
                    observe("http_server_duration_seconds", time.monotonic() - clock, **labels)
        finally:
            request_id.reset(token)
