"""TimeoutMiddleware and the server's own cancellation of a request."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI

from fastapicommon.middleware import TimeoutMiddleware

SCOPE: dict[str, Any] = {
    "type": "http",
    "method": "GET",
    "path": "/slow",
    "raw_path": b"/slow",
    "root_path": "",
    "query_string": b"",
    "headers": [],
}


async def test_outer_cancellation_during_unwind_propagates() -> None:
    """Regression: while a timed-out handler was still unwinding, a cancellation of the request
    itself (server shutdown, client gone) was suppressed and a 503 was sent to nobody."""
    unwinding = asyncio.Event()

    async def slow_app(scope: Any, receive: Any, send: Any) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            unwinding.set()
            await asyncio.sleep(0.5)  # slow cleanup after the timeout's cancel
            raise

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    middleware = TimeoutMiddleware(slow_app, timeout_seconds=0.05)
    task = asyncio.ensure_future(middleware(SCOPE, receive, send))
    await unwinding.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sent == []


async def test_timeout_still_answers_503() -> None:
    async def slow_app(scope: Any, receive: Any, send: Any) -> None:
        await asyncio.sleep(10)

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    scope = {**SCOPE, "app": FastAPI()}
    await TimeoutMiddleware(slow_app, timeout_seconds=0.05)(scope, receive, send)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 503
