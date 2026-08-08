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
        "client_secret",
        "credential",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


def validate_secret_references(value: Any) -> None:
    """Reject plaintext credentials before configuration reaches durable state."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS and not _is_secret_reference(item):
                raise ConfigurationError(
                    f"sensitive connection field {key!r} must use an env: or file: reference"
                )
            validate_secret_references(item)
        return
    if isinstance(value, list):
        for item in value:
            validate_secret_references(item)
        return
    if isinstance(value, str) and not _is_secret_reference(value):
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ConfigurationError(
                "connection URLs cannot contain credentials; use secret-reference fields"
            )


class SecretResolver:
    def __init__(self, allowed_file_roots: tuple[Path, ...] = ()) -> None:
        self.allowed_file_roots = tuple(path.resolve() for path in allowed_file_roots)

    def resolve(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if not isinstance(value, str):
            return value
        if value.startswith("env:"):
            name = value[4:]
            if not name:
                raise ConfigurationError("environment secret reference cannot be empty")
            try:
                return os.environ[name]
            except KeyError as error:
                raise ConfigurationError(
                    f"required environment variable {name!r} is not set"
                ) from error
        if value.startswith("file:"):
            return self._resolve_file(value[5:])
        return value

    def _resolve_file(self, reference: str) -> Any:
        raw_path, separator, key = reference.partition("#")
        if not raw_path:
            raise ConfigurationError("file secret reference cannot be empty")
        path = Path(raw_path).resolve()
        if not self.allowed_file_roots or not any(
            path == root or root in path.parents for root in self.allowed_file_roots
        ):
            raise ConfigurationError(f"secret file {path} is outside the configured roots")
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


def _is_secret_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("env:", "file:"))
