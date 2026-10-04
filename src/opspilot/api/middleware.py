"""Pure ASGI correlation, request deadline and bounded body buffering."""

import asyncio
import json
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opspilot.observability import logger, request_id, span

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

        async def correlated_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", correlation.encode())
                ]
            await send(message)

        async def error_response(status: int, code: str) -> None:
            if not started:
                await correlated_send(
                    {
                        "type": "http.response.start",
                        "status": status,
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
            with span("request"):
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
            logger.error("operation=request request_id=%s outcome=internal_error", correlation)
            await error_response(500, "internal_error")
        finally:
            request_id.reset(token)
