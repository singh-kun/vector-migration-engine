"""Secret-reference validation and just-in-time resolution."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from vme.errors import ConfigurationError

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "connection_string",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "private_key",
        "secret",
        "token",
    }
)

_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_authorization",
    "_credential",
    "_password",
    "_passphrase",
    "_private_key",
    "_secret",
    "_secret_key",
    "_token",
)


def validate_secret_references(value: Any) -> None:
    """Reject plaintext credentials before configuration reaches durable state."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if _is_sensitive_key(normalized) and not is_secret_reference(item):
                raise ConfigurationError(
                    f"sensitive connection field {key!r} must use an env: or file: reference"
                )
            validate_secret_references(item)
        return
    if isinstance(value, list):
        for item in value:
            validate_secret_references(item)
        return
    if isinstance(value, str) and not is_secret_reference(value):
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError(
                "connection URLs cannot contain credentials; use secret-reference fields"
            )


class SecretResolver:
    def __init__(
        self,
        allowed_file_roots: tuple[Path, ...] = (),
        allowed_environment_names: tuple[str, ...] = (),
    ) -> None:
        self.allowed_file_roots = tuple(path.resolve() for path in allowed_file_roots)
        self.allowed_environment_names = frozenset(allowed_environment_names)

    def validate_references(self, value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                self.validate_references(item)
            return
        if isinstance(value, list):
            for item in value:
                self.validate_references(item)
            return
        if not isinstance(value, str):
            return
        if value.startswith("env:"):
            self._validate_env_name(value[4:])
        elif value.startswith("file:"):
            self._validated_file_path(value[5:])

    def resolve(self, value: Any, *, secret_values: set[str] | None = None) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): self.resolve(item, secret_values=secret_values)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.resolve(item, secret_values=secret_values) for item in value]
        if not isinstance(value, str):
            return value
        if value.startswith("env:"):
            name = value[4:]
            self._validate_env_name(name)
            try:
                resolved = os.environ[name]
            except KeyError as error:
                raise ConfigurationError(
                    f"required environment variable {name!r} is not set"
                ) from error
            _record_secret(resolved, secret_values)
            return resolved
        if value.startswith("file:"):
            resolved = self._resolve_file(value[5:])
            _record_secret(resolved, secret_values)
            return resolved
        return value

    def _resolve_file(self, reference: str) -> Any:
        raw_path, separator, key = reference.partition("#")
        path = self._validated_file_path(reference)
        if not path.is_file():
            raise ConfigurationError(f"secret file {path} is not a regular file")
        if path.stat().st_size > 64 * 1024:
            raise ConfigurationError(f"secret file {path} exceeds the 64 KiB limit")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(f"cannot read secret file {path}: {error}") from error
        if not separator:
            return content.rstrip("\r\n")
        try:
            document = json.loads(content)
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"secret file {path} is not valid JSON") from error
        if not isinstance(document, Mapping) or key not in document:
            raise ConfigurationError(f"secret key {key!r} does not exist in {path}")
        return document[key]

    def _validate_env_name(self, name: str) -> None:
        if not name:
            raise ConfigurationError("environment secret reference cannot be empty")
        if name not in self.allowed_environment_names:
            raise ConfigurationError(
                f"environment secret {name!r} is not in VME_SECRET_ENV_ALLOWLIST"
            )

    def _validated_file_path(self, reference: str) -> Path:
        raw_path = reference.partition("#")[0]
        if not raw_path:
            raise ConfigurationError("file secret reference cannot be empty")
        path = Path(raw_path).resolve()
        if not self.allowed_file_roots or not any(
            path == root or root in path.parents for root in self.allowed_file_roots
        ):
            raise ConfigurationError(f"secret file {path} is outside the configured roots")
        return path


def is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("env:", "file:"))


def reject_secret_material(value: Any) -> None:
    """Reject credentials entirely from resources that have no secret-bearing contract."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if _is_sensitive_key(normalized):
                raise ConfigurationError(
                    f"sensitive field {key!r} is allowed only in a connection profile"
                )
            reject_secret_material(item)
        return
    if isinstance(value, list):
        for item in value:
            reject_secret_material(item)
        return
    if isinstance(value, str):
        if is_secret_reference(value):
            raise ConfigurationError("secret references are allowed only in a connection profile")
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError("configuration URLs cannot contain credentials")


def _is_sensitive_key(value: str) -> bool:
    return value in _SENSITIVE_KEYS or value.endswith(_SENSITIVE_SUFFIXES)


def _record_secret(value: Any, secret_values: set[str] | None) -> None:
    if secret_values is not None and value is not None:
        text = str(value)
        if text:
            secret_values.add(text)
