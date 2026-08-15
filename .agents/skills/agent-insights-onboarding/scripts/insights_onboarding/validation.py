"""Validation shared by planning and live execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import unquote, urlparse

from .errors import OnboardingError
from .models import OnboardingPlan

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_RUN_ID = re.compile(r"^[a-z0-9]{12}$")


def normalize_name(value: str, *, max_length: int = 40) -> str:
    selected = re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")
    selected = re.sub(r"-+", "-", selected)[:max_length].rstrip("-")
    if not selected or _NAME.fullmatch(selected) is None:
        raise OnboardingError(
            "invalid_name",
            "Name must contain lowercase letters or numbers after normalization.",
        )
    return selected


def validate_run_id(value: str) -> str:
    if _RUN_ID.fullmatch(value) is None:
        raise OnboardingError("invalid_run_id", "Run ID has an invalid format.")
    return value


def validate_project_endpoint(
    value: str,
    expected_project_name: str | None = None,
    expected_account_name: str | None = None,
) -> str:
    endpoint = value.strip().rstrip("/")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".services.ai.azure.com")
        or parsed.query
        or parsed.fragment
    ):
        raise OnboardingError(
            "invalid_project_endpoint",
            "Project endpoint must be an Azure public-cloud HTTPS project endpoint.",
        )
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if len(segments) != 3 or segments[:2] != ["api", "projects"]:
        raise OnboardingError(
            "invalid_project_endpoint",
            "Project endpoint path must be '/api/projects/<project>'.",
        )
    if expected_project_name and segments[2].casefold() != expected_project_name.casefold():
        raise OnboardingError(
            "project_endpoint_mismatch",
            "Project endpoint name does not match the selected ARM project.",
        )
    if expected_account_name:
        expected_host = f"{expected_account_name}.services.ai.azure.com"
        if parsed.hostname.casefold() != expected_host.casefold():
            raise OnboardingError(
                "project_endpoint_mismatch",
                "Project endpoint host does not match the selected Foundry account.",
            )
    return endpoint


def require_owned_tags(
    tags: object,
    *,
    run_id: str,
    owner_object_id: str,
) -> None:
    if not isinstance(tags, Mapping):
        raise OnboardingError(
            "ownership_mismatch",
            "Resource does not contain ownership tags.",
        )
    expected = {
        "created-by": "agent-insights-quickstart",
        "run-id": run_id,
        "owner-object-id": owner_object_id,
    }
    actual = {str(key).casefold(): str(value) for key, value in tags.items()}
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise OnboardingError(
            "ownership_mismatch",
            "Resource ownership tags do not match this run.",
            {"mismatches": mismatches},
        )


def validate_plan_context(
    plan: OnboardingPlan,
    *,
    subscription_id: str,
    tenant_id: str,
    user_object_id: str,
) -> None:
    context = plan.azure_context
    expected = {
        "subscription_id": subscription_id,
        "tenant_id": tenant_id,
        "user_object_id": user_object_id,
    }
    mismatches = {
        key: {"planned": context.get(key), "actual": value}
        for key, value in expected.items()
        if str(context.get(key) or "").casefold() != value.casefold()
    }
    if mismatches:
        raise OnboardingError(
            "plan_context_changed",
            "Azure context changed after the plan was created.",
            {"mismatches": mismatches},
        )
