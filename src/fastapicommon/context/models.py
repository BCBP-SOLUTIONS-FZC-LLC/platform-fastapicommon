from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated principal for the current request, if any."""

    user_id: str | None = None
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_any_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)

    def has_all_roles(self, *roles: str) -> bool:
        return all(role in self.roles for role in roles)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Per-request platform context: identity plus request/correlation/trace ids and client IP.

    `trace_id` and `client_ip` exist for parity with the Go `gincommon` library's
    `RequestContext{TenantID, UserID, Roles, TraceID, ClientIP}` — a Go service and a Python
    service must expose the same five semantic values to code that inspects the current
    request's context. `trace_id` defaults to `""` (no active/valid OTel span, matching the rest
    of this workspace's "empty string means no trace" convention); `client_ip` defaults to
    `None` when it cannot be determined.
    """

    request_id: str
    correlation_id: str
    user: CurrentUser = field(default_factory=CurrentUser)
    trace_id: str = ""
    client_ip: str | None = None
    #: Why the gateway identity was rejected (``"ambiguous x-user-id header"``,
    #: ``"ambiguous x-tenant-id header"``, ``"invalid tenant roles"``), else ``None``. A rejected
    #: identity is absent from ``user``; ``require_authenticated_user`` answers it with Go's 401.
    identity_rejection: str | None = None

    @property
    def user_id(self) -> str | None:
        return self.user.user_id

    @property
    def tenant_id(self) -> str | None:
        return self.user.tenant_id

    @property
    def roles(self) -> tuple[str, ...]:
        return self.user.roles
