"""ASGI scope keys the platform middleware share within one request."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

#: Set by ``UnhandledErrorMiddleware`` when an exception arrives after the response started:
#: the response is cut short (Go: re-panic with ``http.ErrAbortHandler``), and the access log
#: and server span record the request as aborted with the status already sent.
ABORTED = "fastapicommon.aborted"

#: Set by ``UnhandledErrorMiddleware`` when it answered an unclassified exception itself and
#: marked the server span (``exception`` event, status ``panic``): ``TracingMiddleware`` keeps
#: that status instead of setting the 500's status text.
PANICKED = "fastapicommon.panicked"

#: Go ``StatusClientClosedRequest`` (nginx's 499): the status recorded for an aborted request
#: before any response was written.
STATUS_CLIENT_CLOSED_REQUEST = 499


def request_outcome(
    scope: Mapping[str, Any], status: int, error: BaseException | None
) -> tuple[int, bool, bool]:
    """``(status, aborted, panicked)`` of a finished HTTP request, one rule for the tracing,
    logging and metrics middleware (Go's ``abortedStatus`` / panic handling):

    - aborted: the response was cut short (:data:`ABORTED`) or the request was cancelled (a
      client disconnect, server shutdown): the status already sent, else 499; never a panic;
    - panicked: an exception escaped before any response started: 500;
    - otherwise the status sent (500 if the app returned without a response).
    """
    if scope.get(ABORTED) or isinstance(error, asyncio.CancelledError):
        return status or STATUS_CLIENT_CLOSED_REQUEST, True, False
    if error is not None and not status:
        return 500, False, True
    return status or 500, False, False
