"""Canonical header names for platform authentication and request context.

These headers are read from inbound requests by fastapicommon.middleware and propagated
on outbound calls by fastapicommon.propagation. Service code should reference these
constants rather than hardcoding header strings.
"""

USER_ID_HEADER = "x-user-id"
TENANT_ID_HEADER = "x-tenant-id"
TENANT_ROLES_HEADER = "x-tenant-roles"
REQUEST_ID_HEADER = "x-request-id"
CORRELATION_ID_HEADER = "x-correlation-id"
TRACEPARENT_HEADER = "traceparent"

TENANT_ROLES_SEPARATOR = ","
