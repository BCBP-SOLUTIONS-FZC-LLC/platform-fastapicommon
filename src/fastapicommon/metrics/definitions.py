from __future__ import annotations

from prometheus_client import Counter, Histogram

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total number of HTTP requests processed",
    ["method", "path", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)

GRPC_REQUESTS_TOTAL = Counter(
    "grpc_requests_total",
    "Total number of gRPC requests processed",
    ["method", "status_code"],
)

GRPC_REQUEST_DURATION_SECONDS = Histogram(
    "grpc_request_duration_seconds",
    "gRPC request duration in seconds",
    ["method"],
)


def record_grpc_request(method: str, status_code: str, duration_seconds: float) -> None:
    """Record a completed gRPC call. Used by fastapicommon.grpc server interceptors."""
    GRPC_REQUESTS_TOTAL.labels(method=method, status_code=status_code).inc()
    GRPC_REQUEST_DURATION_SECONDS.labels(method=method).observe(duration_seconds)
