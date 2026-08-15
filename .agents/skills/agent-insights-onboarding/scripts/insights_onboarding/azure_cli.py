"""Small, injectable Azure CLI boundary."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import OnboardingError

_REDACTIONS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(?i)((?:connectionstring|accountkey|clientsecret)\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)([?&](?:sig|token|key)=)([^&\s]+)"),
)


@dataclass(frozen=True, slots=True)
class CommandOutput:
    returncode: int
    stdout: str
    stderr: str


Executor = Callable[[Sequence[str], float], CommandOutput]


def sanitize_text(value: str, *, limit: int = 2000) -> str:
    sanitized = value
    for pattern in _REDACTIONS:
        sanitized = pattern.sub(r"\1******", sanitized)
    return sanitized.strip()[:limit]


def _subprocess_executor(command: Sequence[str], timeout: float) -> CommandOutput:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise OnboardingError(
            "azure_cli_missing",
            "Azure CLI executable 'az' was not found.",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise OnboardingError(
            "azure_cli_timeout",
            "Azure CLI did not complete within the bounded timeout.",
            {"timeout_seconds": timeout},
        ) from error
    return CommandOutput(completed.returncode, completed.stdout, completed.stderr)


class AzureCli:
    def __init__(self, executor: Executor | None = None) -> None:
        self._executor = executor or _subprocess_executor
        if executor is not None:
            self._executable = "az"
        else:
            candidate = "az.cmd" if os.name == "nt" else "az"
            self._executable = shutil.which(candidate) or candidate

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout: float = 120,
        allow_failure: bool = False,
    ) -> CommandOutput:
        command = [self._executable, *arguments]
        result = self._executor(command, timeout)
        if result.returncode and not allow_failure:
            raise OnboardingError(
                "azure_cli_failed",
                "Azure CLI command failed.",
                {
                    "command": ["az", *self._safe_arguments(arguments)],
                    "stderr": sanitize_text(result.stderr),
                    "returncode": result.returncode,
                },
            )
        return result

    def json(
        self,
        arguments: Sequence[str],
        *,
        timeout: float = 120,
        allow_failure: bool = False,
        allow_empty: bool = False,
    ) -> Any:
        result = self.run(
            [*arguments, "--output", "json"],
            timeout=timeout,
            allow_failure=allow_failure,
        )
        if result.returncode and allow_failure:
            return None
        if allow_empty and not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise OnboardingError(
                "invalid_azure_cli_json",
                "Azure CLI returned malformed JSON.",
                {"command": ["az", *self._safe_arguments(arguments)]},
            ) from error

    def account_show(self) -> Mapping[str, Any]:
        value = self.json(["account", "show"])
        if not isinstance(value, Mapping):
            raise OnboardingError(
                "invalid_azure_context",
                "Azure CLI account context was not an object.",
            )
        return value

    def set_subscription(self, subscription_id: str) -> None:
        self.run(["account", "set", "--subscription", subscription_id])

    def signed_in_user(self) -> Mapping[str, Any]:
        value = self.json(["ad", "signed-in-user", "show"])
        if not isinstance(value, Mapping):
            raise OnboardingError(
                "invalid_azure_user",
                "Azure CLI signed-in user response was not an object.",
            )
        return value

    def rest(
        self,
        *,
        method: str,
        url: str,
        body: Mapping[str, Any] | None = None,
        timeout: float = 120,
        allow_failure: bool = False,
    ) -> Any:
        arguments = ["rest", "--method", method, "--url", url]
        if body is not None:
            arguments.extend(
                (
                    "--headers",
                    "Content-Type=application/json",
                    "--body",
                    json.dumps(body, separators=(",", ":")),
                )
            )
        return self.json(
            arguments,
            timeout=timeout,
            allow_failure=allow_failure,
            allow_empty=method.casefold() == "delete",
        )

    @staticmethod
    def _safe_arguments(arguments: Sequence[str]) -> list[str]:
        safe: list[str] = []
        redact_next = False
        for argument in arguments:
            if redact_next:
                safe.append("******")
                redact_next = False
                continue
            safe.append(argument)
            redact_next = argument.casefold() in {
                "--body",
                "--headers",
                "--password",
                "--client-secret",
            }
        return safe
