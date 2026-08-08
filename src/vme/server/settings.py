"""Environment-driven service settings with safe deployment defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServerSettings:
    state_path: Path = Path(".vme/service.sqlite3")
    host: str = "127.0.0.1"
    port: int = 8080
    auth_mode: str = "token"
    api_token: str | None = None
    workspace_id: str = "default"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_workspace_claim: str = "workspace_id"
    oidc_roles_claim: str = "roles"
    run_worker: bool = True
    worker_poll_seconds: float = 0.5
    lease_seconds: int = 30
    allowed_secret_roots: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls) -> ServerSettings:
        roots = tuple(
            Path(item).resolve()
            for item in os.environ.get("VME_SECRET_FILE_ROOTS", "").split(os.pathsep)
            if item
        )
        return cls(
            state_path=Path(os.environ.get("VME_STATE_PATH", ".vme/service.sqlite3")),
            host=os.environ.get("VME_HOST", "127.0.0.1"),
            port=int(os.environ.get("VME_PORT", "8080")),
            auth_mode=os.environ.get("VME_AUTH_MODE", "token").lower(),
            api_token=os.environ.get("VME_API_TOKEN"),
            workspace_id=os.environ.get("VME_WORKSPACE_ID", "default"),
            oidc_issuer=os.environ.get("VME_OIDC_ISSUER"),
            oidc_audience=os.environ.get("VME_OIDC_AUDIENCE"),
            oidc_jwks_url=os.environ.get("VME_OIDC_JWKS_URL"),
            oidc_workspace_claim=os.environ.get("VME_OIDC_WORKSPACE_CLAIM", "workspace_id"),
            oidc_roles_claim=os.environ.get("VME_OIDC_ROLES_CLAIM", "roles"),
            run_worker=_boolean_env("VME_RUN_WORKER", True),
            worker_poll_seconds=float(os.environ.get("VME_WORKER_POLL_SECONDS", "0.5")),
            lease_seconds=int(os.environ.get("VME_LEASE_SECONDS", "30")),
            allowed_secret_roots=roots,
        )

    def validate(self) -> None:
        if self.auth_mode not in {"none", "token", "oidc"}:
            raise ValueError("VME_AUTH_MODE must be 'none', 'token', or 'oidc'")
        if self.auth_mode == "none" and self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("authentication can be disabled only on a loopback bind")
        if self.auth_mode == "token" and not self.api_token:
            raise ValueError("VME_API_TOKEN is required when token authentication is enabled")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError(
                "OIDC authentication requires VME_OIDC_ISSUER, VME_OIDC_AUDIENCE, "
                "and VME_OIDC_JWKS_URL"
            )
        if self.port < 1 or self.port > 65535:
            raise ValueError("VME_PORT must be between 1 and 65535")
        if self.lease_seconds < 5:
            raise ValueError("VME_LEASE_SECONDS must be at least 5")


def _boolean_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")
