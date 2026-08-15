"""Atomic, secret-free receipt persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import OnboardingError

_SECRET_KEY = re.compile(
    r"(?:access|refresh|identity)?token|authorization|connection.?string|"
    r"(?:api|account|client)?key|client.?secret|password|credential",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"InstrumentationKey\s*=|AccountKey\s*=|SharedAccessSignature\s*=|"
    r"EndpointSuffix\s*=|sig=[A-Za-z0-9%._~+/=-]{12,})",
    re.IGNORECASE,
)


def ensure_secret_free(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _SECRET_KEY.search(key):
                raise OnboardingError(
                    "secret_in_receipt",
                    f"Receipt field '{path}.{key}' is not allowed.",
                )
            ensure_secret_free(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            ensure_secret_free(child, f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise OnboardingError(
            "secret_in_receipt",
            f"Receipt value at '{path}' resembles a credential.",
        )


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_secret_free(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise OnboardingError(
            "receipt_not_found",
            f"Receipt does not exist: {path}",
        ) from error
    except json.JSONDecodeError as error:
        raise OnboardingError(
            "invalid_receipt",
            f"Receipt is not valid JSON: {path}",
        ) from error
    if not isinstance(value, dict):
        raise OnboardingError("invalid_receipt", f"Receipt must be an object: {path}")
    ensure_secret_free(value)
    return value
