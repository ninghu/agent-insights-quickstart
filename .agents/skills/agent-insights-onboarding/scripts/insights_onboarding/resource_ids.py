"""Azure resource ID parsing without Azure SDK side effects."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import OnboardingError

_RESOURCE_ID = re.compile(
    r"^/subscriptions/(?P<subscription>[^/]+)/resourceGroups/(?P<resource_group>[^/]+)"
    r"/providers/(?P<namespace>[^/]+)/(?P<tail>.+)$",
    re.IGNORECASE,
)
_GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AzureResourceId:
    raw: str
    subscription_id: str
    resource_group: str
    namespace: str
    types: tuple[str, ...]
    names: tuple[str, ...]

    @property
    def resource_type(self) -> str:
        return f"{self.namespace}/{'/'.join(self.types)}"

    @property
    def name(self) -> str:
        return self.names[-1]

    @property
    def resource_group_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
        )

    def parent(self) -> AzureResourceId:
        if len(self.types) <= 1:
            raise OnboardingError(
                "invalid_resource_parent",
                f"Resource '{self.raw}' has no parent resource.",
            )
        segments: list[str] = []
        for resource_type, name in zip(self.types[:-1], self.names[:-1], strict=True):
            segments.extend((resource_type, name))
        raw = (
            f"{self.resource_group_id}/providers/{self.namespace}/"
            + "/".join(segments)
        )
        return parse_resource_id(raw)


def normalize_resource_id(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith("/"):
        raise OnboardingError(
            "invalid_resource_id",
            "Azure resource IDs must begin with '/'.",
        )
    return normalized


def parse_resource_id(value: str) -> AzureResourceId:
    normalized = normalize_resource_id(value)
    match = _RESOURCE_ID.fullmatch(normalized)
    if match is None:
        raise OnboardingError(
            "invalid_resource_id",
            "Azure resource ID does not match the expected subscription/resource-group shape.",
        )
    subscription_id = match.group("subscription")
    if _GUID.fullmatch(subscription_id) is None:
        raise OnboardingError(
            "invalid_subscription_id",
            "The subscription segment of the resource ID is not a GUID.",
        )
    tail = match.group("tail").split("/")
    if len(tail) < 2 or len(tail) % 2:
        raise OnboardingError(
            "invalid_resource_id",
            "Azure resource ID has unmatched type/name segments.",
        )
    return AzureResourceId(
        raw=normalized,
        subscription_id=subscription_id,
        resource_group=match.group("resource_group"),
        namespace=match.group("namespace"),
        types=tuple(tail[0::2]),
        names=tuple(tail[1::2]),
    )


def require_resource_type(value: str, expected: str) -> AzureResourceId:
    parsed = parse_resource_id(value)
    if parsed.resource_type.casefold() != expected.casefold():
        raise OnboardingError(
            "unexpected_resource_type",
            f"Expected resource type '{expected}', received '{parsed.resource_type}'.",
        )
    return parsed
