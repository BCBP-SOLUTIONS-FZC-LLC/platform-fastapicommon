from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from fastapicommon.telemetry import instrument_grpc_client, instrument_grpc_server, setup_tracing


def test_setup_tracing_returns_tracer_provider() -> None:
    app = FastAPI()
    provider = setup_tracing(app, service_name="test-service")
    assert isinstance(provider, TracerProvider)


def test_instrument_grpc_server_and_client_do_not_raise() -> None:
    instrument_grpc_server()
    instrument_grpc_client()
