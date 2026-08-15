"""Typed, sanitized errors returned by the onboarding CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class OnboardingError(RuntimeError):
    """A failure safe to serialize after its fields pass receipt validation."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 1

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
