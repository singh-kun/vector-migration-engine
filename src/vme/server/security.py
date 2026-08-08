"""Service-boundary validation and HTTP defense-in-depth controls."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vme.errors import ConfigurationError
from vme.server.secrets import is_secret_reference, reject_secret_material

_ASCII_HOSTNAME = re.compile(r"^[a-z0-9.-]+$")


@dataclass(frozen=True, slots=True)
class EndpointRule:
    host: str | None
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None
    port: int

    def permits(self, host: str, port: int) -> bool:
        if port != self.port:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return self.host == _normalize_hostname(host)
        return self.network is not None and address in self.network


class EndpointPolicy:
    """Allow only explicitly configured adapters, paths, and network endpoints."""

    def __init__(
        self,
        *,
        allowed_adapters: Sequence[str],
        allowed_data_roots: Sequence[Path],
        allowed_endpoints: Sequence[str],
        allow_insecure_endpoints: bool,
        allow_embedded_chroma: bool = False,
    ) -> None:
        self.allowed_adapters = frozenset(allowed_adapters)
        self.allowed_data_roots = tuple(path.resolve() for path in allowed_data_roots)
        self.allowed_endpoints = tuple(_parse_endpoint_rule(item) for item in allowed_endpoints)
        self.allow_insecure_endpoints = allow_insecure_endpoints
        self.allow_embedded_chroma = allow_embedded_chroma

    def validate_connection(self, adapter: str, connection: Mapping[str, Any]) -> None:
        if adapter not in self.allowed_adapters:
            raise ConfigurationError(f"adapter {adapter!r} is not enabled for service use")
        if adapter == "memory":
            if connection:
                raise ConfigurationError("memory adapter does not accept connection settings")
            return
        if adapter == "qdrant":
            self._validate_qdrant(connection)
            return
        if adapter == "chroma":
            self._validate_chroma(connection)
            return
        raise ConfigurationError(f"adapter {adapter!r} has no hardened service connection schema")

    def _validate_qdrant(self, connection: Mapping[str, Any]) -> None:
        _reject_unknown(connection, {"path", "url", "timeout", "api_key"}, "Qdrant")
        has_path = bool(connection.get("path"))
        has_url = bool(connection.get("url"))
        if has_path == has_url:
            raise ConfigurationError("Qdrant service profile requires exactly one of path or url")
        if has_path:
            if connection.get("api_key"):
                raise ConfigurationError("local Qdrant path cannot use api_key")
            self._validate_path(connection["path"])
            return
        parsed = _safe_http_url(connection["url"])
        if parsed.scheme != "https" and not self.allow_insecure_endpoints:
            raise ConfigurationError("Qdrant network profiles require HTTPS")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as error:
            raise ConfigurationError("Qdrant URL contains an invalid port") from error
        self._validate_endpoint(str(parsed.hostname), port)
        try:
            timeout = float(connection.get("timeout", 30.0))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("Qdrant timeout must be numeric") from error
        if timeout <= 0 or timeout > 300:
            raise ConfigurationError(
                "Qdrant timeout must be greater than 0 and at most 300 seconds"
            )

    def _validate_chroma(self, connection: Mapping[str, Any]) -> None:
        _reject_unknown(connection, {"path", "host", "port", "ssl", "headers"}, "Chroma")
        has_path = bool(connection.get("path"))
        has_host = bool(connection.get("host"))
        if has_path == has_host:
            raise ConfigurationError("Chroma service profile requires exactly one of path or host")
        if has_path:
            if not self.allow_embedded_chroma:
                raise ConfigurationError(
                    "embedded Chroma is disabled; use the remote thin-client profile"
                )
            if any(key in connection for key in ("port", "ssl", "headers")):
                raise ConfigurationError("local Chroma path cannot use network settings")
            self._validate_path(connection["path"])
            return
        if not bool(connection.get("ssl", False)) and not self.allow_insecure_endpoints:
            raise ConfigurationError("Chroma network profiles require TLS")
        host = str(connection["host"])
        try:
            port = int(connection.get("port", 8000))
        except (TypeError, ValueError) as error:
            raise ConfigurationError("Chroma port must be an integer") from error
        self._validate_endpoint(host, port)
        headers = connection.get("headers", {})
        if not isinstance(headers, Mapping):
            raise ConfigurationError("Chroma headers must be an object")
        for name, value in headers.items():
            if not _valid_header_name(str(name)):
                raise ConfigurationError("Chroma header names contain unsafe characters")
            if not is_secret_reference(value):
                raise ConfigurationError(
                    "Chroma header values must use env: or file: secret references"
                )

    def _validate_path(self, value: Any) -> None:
        path = Path(str(value)).resolve()
        if not self.allowed_data_roots or not any(
            path == root or root in path.parents for root in self.allowed_data_roots
        ):
            raise ConfigurationError("local database path is outside configured data roots")

    def _validate_endpoint(self, host: str, port: int) -> None:
        if port < 1 or port > 65535:
            raise ConfigurationError("endpoint port must be between 1 and 65535")
        try:
            permitted = any(rule.permits(host, port) for rule in self.allowed_endpoints)
        except ValueError as error:
            raise ConfigurationError("endpoint hostname is invalid") from error
        if not permitted:
            raise ConfigurationError("network endpoint is not in VME_ENDPOINT_ALLOWLIST")


class RequestBodyLimitMiddleware:
    """Reject oversized bodies for both Content-Length and streamed requests."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        raw_length = headers.get(b"content-length")
        has_body = raw_length not in {None, b"0"} or b"transfer-encoding" in headers
        if scope.get("method") in {"POST", "PUT", "PATCH"} and has_body:
            content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if content_type != b"application/json":
                await _limit_response(
                    415,
                    "request bodies must use application/json",
                    scope,
                    receive,
                    send,
                )
                return
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                await _limit_response(400, "invalid Content-Length header", scope, receive, send)
                return
            if length < 0 or length > self.max_bytes:
                await _limit_response(
                    413,
                    "request body exceeds the configured limit",
                    scope,
                    receive,
                    send,
                )
                return

        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise _RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestTooLarge:
            await _limit_response(
                413,
                "request body exceeds the configured limit",
                scope,
                receive,
                send,
            )


class SecurityHeadersMiddleware:
    """Prevent caching, framing, and MIME sniffing of API responses."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                if self.hsts:
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        await self.app(scope, receive, secure_send)


def validate_migration_payload(value: Mapping[str, Any]) -> None:
    """Migration definitions may select resources but must never carry credentials."""

    reject_secret_material(value)
    _validate_shape(value, depth=0)


def _validate_shape(value: Any, *, depth: int) -> None:
    if depth > 20:
        raise ConfigurationError("migration configuration nesting exceeds 20 levels")
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_shape(item, depth=depth + 1)
    elif isinstance(value, str) and len(value) > 16_384:
        raise ConfigurationError("migration configuration strings cannot exceed 16384 characters")


def _parse_endpoint_rule(value: str) -> EndpointRule:
    raw = value.strip()
    host_part, separator, port_part = raw.rpartition(":")
    if not separator or not host_part or not port_part.isdigit():
        raise ValueError("VME_ENDPOINT_ALLOWLIST entries must use host-or-CIDR:port")
    port = int(port_part)
    if port < 1 or port > 65535:
        raise ValueError("VME_ENDPOINT_ALLOWLIST ports must be between 1 and 65535")
    if host_part.startswith("[") and host_part.endswith("]"):
        host_part = host_part[1:-1]
    try:
        network = ipaddress.ip_network(host_part, strict=False)
    except ValueError:
        return EndpointRule(_normalize_hostname(host_part), None, port)
    return EndpointRule(None, network, port)


def _normalize_hostname(value: str) -> str:
    candidate = value.strip().rstrip(".")
    if not candidate or len(candidate) > 253 or any(char in candidate for char in "/\\@#"):
        raise ValueError("invalid endpoint hostname")
    try:
        normalized = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("invalid endpoint hostname") from error
    if not _ASCII_HOSTNAME.fullmatch(normalized) or ".." in normalized:
        raise ValueError("invalid endpoint hostname")
    return normalized


def _safe_http_url(value: Any) -> Any:
    parsed = urlsplit(str(value))
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise ConfigurationError("endpoint URL must be an HTTP(S) URL without credentials or query")
    return parsed


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(str(item) for item in unknown))
        raise ConfigurationError(f"{label} connection contains unsupported fields: {names}")


def _valid_header_name(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and all(char.isalnum() or char in "!#$%&'*+-.^_`|~" for char in value)
    )


class _RequestTooLarge(Exception):
    pass


async def _limit_response(
    status_code: int,
    detail: str,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    response = JSONResponse(
        {
            "type": "about:blank",
            "title": "Invalid request",
            "status": status_code,
            "detail": detail,
        },
        status_code=status_code,
        media_type="application/problem+json",
        headers={"Connection": "close"},
    )
    await response(scope, receive, send)
