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
    """Per-request platform context: identity plus request/correlation ids."""

    request_id: str
    correlation_id: str
    user: CurrentUser = field(default_factory=CurrentUser)

    @property
    def user_id(self) -> str | None:
        return self.user.user_id

    @property
    def tenant_id(self) -> str | None:
        return self.user.tenant_id

    @property
    def roles(self) -> tuple[str, ...]:
        return self.user.roles
