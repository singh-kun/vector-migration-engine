"""Local-token and OIDC authentication with workspace-scoped roles."""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from typing import Any

from vme.errors import ConfigurationError
from vme.server.models import Actor, WorkspaceRole
from vme.server.settings import ServerSettings

_ROLE_ORDER = {
    WorkspaceRole.VIEWER: 0,
    WorkspaceRole.OPERATOR: 1,
    WorkspaceRole.ADMIN: 2,
}


class AuthenticationError(Exception):
    """A request does not carry a valid VME identity."""


class Authenticator:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self._jwks_client: Any | None = None
        if settings.auth_mode == "oidc":
            try:
                import jwt
            except ImportError as error:
                raise ConfigurationError(
                    "OIDC mode requires `pip install vector-migration-engine[server]`"
                ) from error
            self._jwt = jwt
            self._jwks_client = jwt.PyJWKClient(str(settings.oidc_jwks_url), cache_keys=True)

    def authenticate(self, authorization: str | None) -> Actor:
        if self.settings.auth_mode == "none":
            return Actor("local", self.settings.workspace_id, WorkspaceRole.ADMIN)
        token = _bearer_token(authorization)
        if self.settings.auth_mode == "token":
            assert self.settings.api_token is not None
            if not hmac.compare_digest(token, self.settings.api_token):
                raise AuthenticationError("invalid bearer token")
            return Actor("local-token", self.settings.workspace_id, WorkspaceRole.ADMIN)
        return self._authenticate_oidc(token)

    def _authenticate_oidc(self, token: str) -> Actor:
        assert self._jwks_client is not None
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = self._jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.settings.oidc_audience,
                issuer=self.settings.oidc_issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except Exception as error:
            raise AuthenticationError("OIDC token validation failed") from error
        workspace = claims.get(self.settings.oidc_workspace_claim)
        if not isinstance(workspace, str) or not workspace:
            raise AuthenticationError("OIDC token has no valid workspace claim")
        role = _highest_role(claims.get(self.settings.oidc_roles_claim))
        return Actor(str(claims["sub"]), workspace, role)


def require_role(actor: Actor, required: WorkspaceRole) -> None:
    if _ROLE_ORDER[actor.role] < _ROLE_ORDER[required]:
        raise PermissionError(f"{required.value} role is required")


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthenticationError("missing bearer token")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise AuthenticationError("authorization must use the Bearer scheme")
    return token


def _highest_role(value: Any) -> WorkspaceRole:
    raw_roles: Sequence[Any]
    if isinstance(value, str):
        raw_roles = [value]
    elif isinstance(value, Sequence):
        raw_roles = value
    else:
        raise AuthenticationError("OIDC token has no valid roles claim")
    roles: list[WorkspaceRole] = []
    for item in raw_roles:
        try:
            roles.append(WorkspaceRole(str(item).lower()))
        except ValueError:
            continue
    if not roles:
        raise AuthenticationError("OIDC token grants no VME workspace role")
    return max(roles, key=_ROLE_ORDER.__getitem__)
