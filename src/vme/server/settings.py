"""Environment-driven service settings with safe deployment defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


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
    allowed_secret_env_names: tuple[str, ...] = ()
    allowed_data_roots: tuple[Path, ...] = ()
    allowed_endpoints: tuple[str, ...] = ()
    allowed_adapters: tuple[str, ...] = ("chroma", "qdrant")
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")
    allow_insecure_endpoints: bool = False
    allow_embedded_chroma: bool = False
    expose_docs: bool = False
    hsts_enabled: bool = False
    max_request_bytes: int = 1024 * 1024
    max_concurrency: int = 100
    timeout_keep_alive: int = 5

    @classmethod
    def from_env(cls) -> ServerSettings:
        roots = tuple(
            Path(item).resolve()
            for item in os.environ.get("VME_SECRET_FILE_ROOTS", "").split(os.pathsep)
            if item
        )
        data_roots = tuple(
            Path(item).resolve()
            for item in os.environ.get("VME_DATA_ROOTS", "").split(os.pathsep)
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
            allowed_secret_env_names=_csv_env("VME_SECRET_ENV_ALLOWLIST"),
            allowed_data_roots=data_roots,
            allowed_endpoints=_csv_env("VME_ENDPOINT_ALLOWLIST"),
            allowed_adapters=_csv_env("VME_ALLOWED_ADAPTERS", ("chroma", "qdrant")),
            allowed_hosts=_csv_env("VME_ALLOWED_HOSTS", ("127.0.0.1", "localhost", "testserver")),
            allow_insecure_endpoints=_boolean_env("VME_ALLOW_INSECURE_ENDPOINTS", False),
            allow_embedded_chroma=_boolean_env("VME_ALLOW_EMBEDDED_CHROMA", False),
            expose_docs=_boolean_env("VME_EXPOSE_DOCS", False),
            hsts_enabled=_boolean_env("VME_HSTS", False),
            max_request_bytes=int(os.environ.get("VME_MAX_REQUEST_BYTES", str(1024 * 1024))),
            max_concurrency=int(os.environ.get("VME_MAX_CONCURRENCY", "100")),
            timeout_keep_alive=int(os.environ.get("VME_TIMEOUT_KEEP_ALIVE", "5")),
        )

    def validate(self, *, require_api_auth: bool = True) -> None:
        if self.auth_mode not in {"none", "token", "oidc"}:
            raise ValueError("VME_AUTH_MODE must be 'none', 'token', or 'oidc'")
        if (
            require_api_auth
            and self.auth_mode == "none"
            and self.host not in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("authentication can be disabled only on a loopback bind")
        if require_api_auth and self.auth_mode == "token":
            if not self.api_token:
                raise ValueError("VME_API_TOKEN is required when token authentication is enabled")
            if len(self.api_token) < 32:
                raise ValueError("VME_API_TOKEN must contain at least 32 characters")
            if len(self.api_token) > 4096 or any(char.isspace() for char in self.api_token):
                raise ValueError("VME_API_TOKEN contains unsafe whitespace or is too long")
        if (
            require_api_auth
            and self.auth_mode == "oidc"
            and not all((self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url))
        ):
            raise ValueError(
                "OIDC authentication requires VME_OIDC_ISSUER, VME_OIDC_AUDIENCE, "
                "and VME_OIDC_JWKS_URL"
            )
        if require_api_auth and self.auth_mode == "oidc":
            _require_https_url(str(self.oidc_issuer), "VME_OIDC_ISSUER")
            _require_https_url(str(self.oidc_jwks_url), "VME_OIDC_JWKS_URL")
        if not _IDENTIFIER.fullmatch(self.workspace_id):
            raise ValueError("VME_WORKSPACE_ID contains invalid characters")
        if self.port < 1 or self.port > 65535:
            raise ValueError("VME_PORT must be between 1 and 65535")
        if self.lease_seconds < 5:
            raise ValueError("VME_LEASE_SECONDS must be at least 5")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError("VME_ALLOWED_HOSTS must be an explicit non-empty allowlist")
        if not self.allowed_adapters or not set(self.allowed_adapters) <= {
            "chroma",
            "qdrant",
            "memory",
        }:
            raise ValueError("VME_ALLOWED_ADAPTERS contains an unsupported service adapter")
        if self.max_request_bytes < 1024 or self.max_request_bytes > 16 * 1024 * 1024:
            raise ValueError("VME_MAX_REQUEST_BYTES must be between 1024 and 16777216")
        if self.max_concurrency < 1 or self.max_concurrency > 10_000:
            raise ValueError("VME_MAX_CONCURRENCY must be between 1 and 10000")
        if self.timeout_keep_alive < 1 or self.timeout_keep_alive > 60:
            raise ValueError("VME_TIMEOUT_KEEP_ALIVE must be between 1 and 60")


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


def _csv_env(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _require_https_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a credential-free HTTPS URL")
