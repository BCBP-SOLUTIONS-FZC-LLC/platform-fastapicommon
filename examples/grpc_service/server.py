"""Example gRPC server built on fastapicommon's gRPC interceptors.

Uses grpc's generic handler API with raw bytes instead of a compiled .proto/pb2 module,
so the example runs with just `pip install fastapicommon[grpc]` and no build step.

Run with: python -m examples.grpc_service.server
"""

from __future__ import annotations

from concurrent import futures

import grpc
from fastapicommon.grpc import ContextServerInterceptor
from fastapicommon.logging import configure_logging, get_logger
from fastapicommon.telemetry import instrument_grpc_server

logger = get_logger(__name__)


def _identity(data: bytes) -> bytes:
    return data


def echo(request_bytes: bytes, context: grpc.ServicerContext) -> bytes:
    logger.info("echo_called", payload_size=len(request_bytes))
    return request_bytes


def create_server() -> grpc.Server:
    configure_logging()
    instrument_grpc_server()

    handler = grpc.unary_unary_rpc_method_handler(
        echo, request_deserializer=_identity, response_serializer=_identity
    )
    generic_handler = grpc.method_handlers_generic_handler("example.EchoService", {"Echo": handler})

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[ContextServerInterceptor()],
    )
    server.add_generic_rpc_handlers((generic_handler,))
    server.add_insecure_port("[::]:50051")
    return server


if __name__ == "__main__":
    grpc_server = create_server()
    grpc_server.start()
    logger.info("grpc_server_started", port=50051)
    grpc_server.wait_for_termination()
