import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace

from fastapicommon.context import get_request_context
from fastapicommon.middleware import ContextMiddleware
from fastapicommon.telemetry import TracingConfig, init_tracing


def _build_app(trusted_proxies: str | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ContextMiddleware, trusted_proxies=trusted_proxies)

    @app.get("/whoami")
    async def whoami() -> dict[str, object]:
        context = get_request_context()
        return {
            "request_id": context.request_id,
            "correlation_id": context.correlation_id,
            "user_id": context.user_id,
            "tenant_id": context.tenant_id,
            "roles": list(context.roles),
            "trace_id": context.trace_id,
            "client_ip": context.client_ip,
            "identity_rejection": context.identity_rejection,
        }

    return app


def test_context_extracted_from_headers() -> None:
    client = TestClient(_build_app())
    response = client.get(
        "/whoami",
        headers={
            "x-user-id": "u1",
            "x-tenant-id": "t1",
            "x-tenant-roles": "admin, operator",
            "x-request-id": "req-123",
            "x-correlation-id": "corr-456",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "req-123"
    assert body["correlation_id"] == "corr-456"
    assert body["user_id"] == "u1"
    assert body["tenant_id"] == "t1"
    assert body["roles"] == ["admin", "operator"]
    assert response.headers["x-request-id"] == "req-123"
    assert response.headers["x-correlation-id"] == "corr-456"
    assert response.headers["X-Request-ID"] == "req-123"


def test_ids_generated_when_absent() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami")
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["correlation_id"] == body["request_id"]
    assert body["user_id"] is None
    assert body["roles"] == []
    assert response.headers["x-request-id"] == body["request_id"]


def test_context_not_leaked_between_requests() -> None:
    client = TestClient(_build_app())
    first = client.get("/whoami", headers={"x-user-id": "u1"}).json()
    second = client.get("/whoami").json()
    assert first["user_id"] == "u1"
    assert second["user_id"] is None
    assert first["request_id"] != second["request_id"]


# --- header validation (ported from Go's gincommon) ---


def test_invalid_request_id_falls_back_to_generated_uuid() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-request-id": "has a space"})
    body = response.json()
    assert body["request_id"] != "has a space"
    assert body["request_id"]


def test_control_character_in_identity_header_is_treated_as_absent() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-user-id": "bad\x01value"})
    assert response.json()["user_id"] is None


def test_oversized_identity_header_is_treated_as_absent() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-tenant-id": "a" * 300})
    assert response.json()["tenant_id"] is None


def test_invalid_role_tokens_are_silently_dropped() -> None:
    client = TestClient(_build_app())
    response = client.get(
        "/whoami", headers={"x-tenant-roles": "admin,has space,operator,has/slash"}
    )
    assert response.json()["roles"] == ["admin", "operator"]


def test_duplicate_role_tokens_are_deduplicated() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-tenant-roles": "admin,operator,admin"})
    assert response.json()["roles"] == ["admin", "operator"]


# --- client_ip ---


XFF = {"x-forwarded-for": "203.0.113.5, 70.41.3.18, 10.0.0.7"}
PROXY_PEER = ("10.0.0.2", 50000)


def test_client_ip_ignores_forwarding_headers_from_an_untrusted_peer() -> None:
    """Regression: X-Forwarded-For / X-Real-IP from any client became the client IP (spoofable).
    Without trusted proxies (the default) the client IP is the socket peer."""
    client = TestClient(_build_app(), client=PROXY_PEER)
    assert client.get("/whoami", headers=XFF).json()["client_ip"] == "10.0.0.2"
    assert client.get("/whoami", headers={"x-real-ip": "6.6.6.6"}).json()["client_ip"] == "10.0.0.2"
    other = TestClient(_build_app("10.0.0.0/8"), client=("198.51.100.4", 1))
    assert other.get("/whoami", headers=XFF).json()["client_ip"] == "198.51.100.4"


def test_client_ip_walks_x_forwarded_for_from_a_trusted_proxy() -> None:
    """Gin ClientIP with SetTrustedProxies: from the right, the first hop that is not a trusted
    proxy (10.0.0.7 is one, so 70.41.3.18 is the client)."""
    client = TestClient(_build_app("10.0.0.0/8"), client=PROXY_PEER)
    assert client.get("/whoami", headers=XFF).json()["client_ip"] == "70.41.3.18"
    every_hop_trusted = {"x-forwarded-for": "10.1.1.1, 10.2.2.2"}
    assert client.get("/whoami", headers=every_hop_trusted).json()["client_ip"] == "10.1.1.1"
    assert client.get("/whoami", headers={"x-real-ip": "203.0.113.9"}).json()["client_ip"] == (
        "203.0.113.9"
    )
    garbage = {"x-forwarded-for": "not-an-ip"}
    assert client.get("/whoami", headers=garbage).json()["client_ip"] == "10.0.0.2"


def test_invalid_trusted_proxy_fails_at_startup() -> None:
    with pytest.raises(ValueError):
        TestClient(_build_app("10.0.0.0/33")).get("/whoami")


@pytest.mark.parametrize("header", ["x-user-id", "x-tenant-id"])
def test_repeated_identity_header_is_ambiguous(header: str) -> None:
    """Regression: the first value won. gincommon rejects a repeated identity header (SEC-3); the
    whole identity is absent here and require_authenticated_user answers 401."""
    client = TestClient(_build_app())
    headers = [("x-user-id", "alice"), ("x-tenant-id", "t1"), (header, "mallory")]
    body = client.get("/whoami", headers=headers).json()
    assert body["user_id"] is None and body["tenant_id"] is None and body["roles"] == []
    assert body["identity_rejection"] == f"ambiguous {header} header"


def test_roles_over_the_caps_reject_the_identity() -> None:
    """Regression: no caps. gincommon: header > 4096 bytes or > 64 distinct roles is invalid."""
    client = TestClient(_build_app())
    too_many = ",".join(f"r{i}" for i in range(65))
    body = client.get("/whoami", headers={"x-user-id": "u1", "x-tenant-roles": too_many}).json()
    assert body["user_id"] is None and body["identity_rejection"] == "invalid tenant roles"
    too_long = "a" * 4097
    body = client.get("/whoami", headers={"x-user-id": "u1", "x-tenant-roles": too_long}).json()
    assert body["identity_rejection"] == "invalid tenant roles"
    at_the_cap = ",".join(f"r{i}" for i in range(64))
    body = client.get("/whoami", headers={"x-user-id": "u1", "x-tenant-roles": at_the_cap}).json()
    assert body["user_id"] == "u1" and len(body["roles"]) == 64


def test_repeated_roles_headers_are_joined() -> None:
    client = TestClient(_build_app())
    headers = [("x-user-id", "u1"), ("x-tenant-roles", "admin"), ("x-tenant-roles", "viewer,admin")]
    assert client.get("/whoami", headers=headers).json()["roles"] == ["admin", "viewer"]


def test_client_ip_falls_back_to_direct_peer() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami")
    assert response.json()["client_ip"]  # TestClient supplies a default peer address


# --- trace_id ---


def test_trace_id_is_empty_string_when_no_active_span() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami")
    assert response.json()["trace_id"] == ""
    assert "X-Trace-ID" not in response.headers


def test_trace_id_echoed_when_a_real_span_is_active() -> None:
    init_tracing(TracingConfig(service_name="test-caller", endpoint=""))
    tracer = otel_trace.get_tracer("test-caller")
    client = TestClient(_build_app())

    with tracer.start_as_current_span("outer-span"):
        response = client.get("/whoami")

    body = response.json()
    assert body["trace_id"]
    assert len(body["trace_id"]) == 32
    assert response.headers["X-Trace-ID"] == body["trace_id"]


@pytest.mark.parametrize(
    "bad", ["has a space", "a" * 129, "<script>", "idé".encode(), "   ", "x;y=z"]
)
def test_invalid_correlation_id_falls_back_to_request_id(bad: str | bytes) -> None:
    """Regression: x-correlation-id was echoed on the response and logged verbatim; it is
    validated like x-request-id now, and an invalid one falls back to the request id."""
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-request-id": "req-1", "x-correlation-id": bad})
    assert response.json()["correlation_id"] == "req-1"
    assert response.headers["x-correlation-id"] == "req-1"


def test_valid_correlation_id_is_kept_with_an_invalid_request_id() -> None:
    client = TestClient(_build_app())
    body = client.get(
        "/whoami", headers={"x-request-id": "bad id", "x-correlation-id": "corr-7"}
    ).json()
    assert body["correlation_id"] == "corr-7" and body["request_id"] not in ("bad id", "corr-7")
