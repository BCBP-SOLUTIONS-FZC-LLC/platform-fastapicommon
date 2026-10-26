"""Shared FastAPI and gRPC middleware library for platform services.

This package owns cross-cutting concerns only (auth context, tenant propagation,
tracing, logging, metrics, timeouts, RBAC, health probes) and must never contain
business logic, service-specific routes, persistence, or domain models.

The ``fastapicommon.grpc`` submodule requires the ``grpc`` extra and is not imported
here to keep the base install free of a grpcio dependency.
"""

from fastapicommon.bootstrap import create_platform_app

__version__ = "0.4.1"

__all__ = ["__version__", "create_platform_app"]
