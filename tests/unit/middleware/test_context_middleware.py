from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace as otel_trace

from fastapicommon.context import get_request_context
from fastapicommon.middleware import ContextMiddleware
from fastapicommon.telemetry import TracingConfig, init_tracing


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ContextMiddleware)

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


def test_client_ip_prefers_x_forwarded_for() -> None:
    client = TestClient(_build_app())
    response = client.get(
        "/whoami", headers={"x-forwarded-for": "203.0.113.5, 70.41.3.18, 150.172.238.178"}
    )
    assert response.json()["client_ip"] == "203.0.113.5"


def test_client_ip_falls_back_to_x_real_ip() -> None:
    client = TestClient(_build_app())
    response = client.get("/whoami", headers={"x-real-ip": "203.0.113.9"})
    assert response.json()["client_ip"] == "203.0.113.9"


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
