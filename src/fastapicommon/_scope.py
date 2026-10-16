"""ASGI scope keys the platform middleware share within one request."""

#: Set by ``UnhandledErrorMiddleware`` when an exception arrives after the response started:
#: the response is cut short (Go: re-panic with ``http.ErrAbortHandler``), and the access log
#: and server span record the request as aborted with the status already sent.
ABORTED = "fastapicommon.aborted"

#: Set by ``UnhandledErrorMiddleware`` when it answered an unclassified exception itself and
#: marked the server span (``exception`` event, status ``panic``): ``TracingMiddleware`` keeps
#: that status instead of setting the 500's status text.
PANICKED = "fastapicommon.panicked"
