"""Read-only Azure and Foundry discovery."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from .azure_cli import AzureCli
from .errors import OnboardingError
from .models import AzureContext
from .resource_ids import parse_resource_id, require_resource_type

_MINIMUM_AZURE_CLI = (2, 80, 0)
_PROJECT_API_VERSION = "2025-06-01"
_CONNECTION_API_VERSION = "2025-04-01-preview"
_APP_INSIGHTS_API_VERSION = "2020-02-02"
_WORKSPACE_TABLE_API_VERSION = "2025-02-01"


def parse_version(value: str) -> tuple[int, int, int]:
    numbers = [int(item) for item in re.findall(r"\d+", value)[:3]]
    numbers.extend([0] * (3 - len(numbers)))
    return tuple(numbers)  # type: ignore[return-value]


def check_azure_cli_version(cli: AzureCli) -> str:
    value = cli.json(["version"])
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "invalid_azure_cli_version",
            "Azure CLI version response was invalid.",
        )
    version = str(value.get("azure-cli") or "")
    if parse_version(version) < _MINIMUM_AZURE_CLI:
        raise OnboardingError(
            "azure_cli_too_old",
            "Azure CLI 2.80 or newer is required.",
            {"found": version, "required": "2.80.0"},
        )
    return version


def select_context(cli: AzureCli, subscription_id: str) -> AzureContext:
    cli.set_subscription(subscription_id)
    account = cli.account_show()
    cloud = cli.json(["cloud", "show"])
    if not isinstance(cloud, Mapping) or str(cloud.get("name") or "") != "AzureCloud":
        raise OnboardingError(
            "unsupported_cloud",
            "Agent Insights Quickstart v1 supports AzureCloud only.",
        )
    if str(account.get("id") or "").casefold() != subscription_id.casefold():
        raise OnboardingError(
            "subscription_context_mismatch",
            "Azure CLI did not switch to the requested subscription.",
        )
    user = account.get("user")
    if not isinstance(user, Mapping) or str(user.get("type") or "").casefold() != "user":
        raise OnboardingError(
            "interactive_user_required",
            "Azure CLI must be signed in as an interactive user.",
        )
    signed_in = cli.signed_in_user()
    object_id = str(signed_in.get("id") or "").strip()
    if not object_id:
        raise OnboardingError(
            "missing_user_object_id",
            "Azure CLI did not return the signed-in user's object ID.",
        )
    return AzureContext(
        cloud="AzureCloud",
        subscription_id=str(account.get("id") or ""),
        subscription_name=str(account.get("name") or ""),
        tenant_id=str(account.get("tenantId") or ""),
        user_name=str(user.get("name") or ""),
        user_type=str(user.get("type") or ""),
        user_object_id=object_id,
    )


def list_subscriptions(cli: AzureCli) -> list[dict[str, Any]]:
    value = cli.json(["account", "list", "--all"])
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_subscription_list",
            "Azure CLI returned an invalid subscription list.",
        )
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "tenant_id": str(item.get("tenantId") or ""),
            "is_default": bool(item.get("isDefault")),
            "state": str(item.get("state") or ""),
        }
        for item in value
        if isinstance(item, Mapping) and item.get("id")
    ]


def provider_states(cli: AzureCli, namespaces: Sequence[str]) -> dict[str, str]:
    states: dict[str, str] = {}
    for namespace in namespaces:
        value = cli.json(["provider", "show", "--namespace", namespace])
        if not isinstance(value, Mapping):
            raise OnboardingError(
                "invalid_provider_state",
                f"Azure provider response was invalid for '{namespace}'.",
            )
        states[namespace] = str(value.get("registrationState") or "")
    return states


def list_projects(cli: AzureCli, subscription_id: str) -> list[dict[str, Any]]:
    value = cli.json(
        [
            "resource",
            "list",
            "--subscription",
            subscription_id,
            "--resource-type",
            "Microsoft.CognitiveServices/accounts/projects",
        ]
    )
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_project_list",
            "Azure CLI returned an invalid Foundry project list.",
        )
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "location": str(item.get("location") or ""),
        }
        for item in value
        if isinstance(item, Mapping) and item.get("id")
    ]


def get_project(cli: AzureCli, project_resource_id: str) -> Mapping[str, Any]:
    require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    value = cli.json(
        [
            "resource",
            "show",
            "--ids",
            project_resource_id,
            "--api-version",
            _PROJECT_API_VERSION,
        ]
    )
    if not isinstance(value, Mapping):
        raise OnboardingError("invalid_project", "Foundry project response was invalid.")
    return value


def derive_project_endpoint(project_resource_id: str) -> str:
    project = require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    account_name, project_name = project.names
    return (
        f"https://{account_name}.services.ai.azure.com/api/projects/"
        f"{quote(project_name, safe='')}"
    )


def list_app_insights_connections(
    cli: AzureCli,
    scope_resource_id: str,
) -> list[dict[str, str]]:
    return [
        item
        for item in list_connections(cli, scope_resource_id)
        if item["category"].casefold() == "appinsights"
    ]


def list_connections(
    cli: AzureCli,
    scope_resource_id: str,
) -> list[dict[str, str]]:
    url = (
        f"https://management.azure.com{scope_resource_id}/connections"
        f"?api-version={_CONNECTION_API_VERSION}"
    )
    value = cli.rest(method="get", url=url)
    if not isinstance(value, Mapping) or not isinstance(value.get("value"), list):
        raise OnboardingError(
            "invalid_connection_list",
            "Foundry project connection response was invalid.",
        )
    connections: list[dict[str, str]] = []
    for item in value["value"]:
        if not isinstance(item, Mapping):
            continue
        properties = item.get("properties")
        if not isinstance(properties, Mapping):
            continue
        category = str(properties.get("category") or "")
        metadata = properties.get("metadata")
        resource_id = (
            str(metadata.get("ResourceId") or metadata.get("resourceId") or "")
            if isinstance(metadata, Mapping)
            else ""
        )
        if resource_id and category.casefold() == "appinsights":
            require_resource_type(resource_id, "Microsoft.Insights/components")
        connections.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "").split("/")[-1],
                "category": category,
                "resource_id": resource_id,
            }
        )
    return connections


def get_application_insights(
    cli: AzureCli,
    resource_id: str,
) -> Mapping[str, Any]:
    require_resource_type(resource_id, "Microsoft.Insights/components")
    value = cli.json(
        [
            "resource",
            "show",
            "--ids",
            resource_id,
            "--api-version",
            _APP_INSIGHTS_API_VERSION,
        ]
    )
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "invalid_application_insights",
            "Application Insights response was invalid.",
        )
    return value


def linked_workspace_id(app_insights: Mapping[str, Any]) -> str:
    properties = app_insights.get("properties")
    if not isinstance(properties, Mapping):
        raise OnboardingError(
            "missing_log_analytics_workspace",
            "Application Insights has no properties object.",
        )
    value = str(
        properties.get("WorkspaceResourceId")
        or properties.get("workspaceResourceId")
        or ""
    ).strip()
    require_resource_type(value, "Microsoft.OperationalInsights/workspaces")
    return value


def trace_table_is_protected(cli: AzureCli, workspace_id: str) -> bool:
    require_resource_type(workspace_id, "Microsoft.OperationalInsights/workspaces")
    url = (
        f"https://management.azure.com{workspace_id}/tables/AppGenAIContent"
        f"?api-version={_WORKSPACE_TABLE_API_VERSION}"
    )
    value = cli.rest(method="get", url=url, allow_failure=True)
    if value is None:
        return False
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "invalid_trace_table",
            "Log Analytics table response was invalid.",
        )
    properties = value.get("properties")
    protection_level = (
        str(properties.get("protectionLevel") or "")
        if isinstance(properties, Mapping)
        else ""
    )
    return protection_level.casefold() == "protected"


def subscription_permissions(cli: AzureCli, subscription_id: str) -> list[Mapping[str, Any]]:
    return permissions_at_scope(cli, f"/subscriptions/{subscription_id}")


def permissions_at_scope(cli: AzureCli, scope: str) -> list[Mapping[str, Any]]:
    url = (
        f"https://management.azure.com{scope}/providers/Microsoft.Authorization/permissions"
        "?api-version=2022-04-01"
    )
    value = cli.rest(method="get", url=url)
    if not isinstance(value, Mapping) or not isinstance(value.get("value"), list):
        raise OnboardingError(
            "invalid_permission_response",
            "Azure effective-permission response was invalid.",
        )
    return [item for item in value["value"] if isinstance(item, Mapping)]


def account_id_from_project(project_resource_id: str) -> str:
    return parse_resource_id(project_resource_id).parent().raw


def model_is_available(
    cli: AzureCli,
    *,
    location: str,
    model_name: str,
    model_version: str,
    model_format: str,
    sku_name: str,
) -> bool:
    value = cli.json(["cognitiveservices", "model", "list", "--location", location])
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_model_catalog",
            "Azure CLI returned an invalid model catalog.",
        )
    for item in value:
        if not isinstance(item, Mapping):
            continue
        model = item.get("model")
        if not isinstance(model, Mapping):
            model = item
        skus = item.get("skus")
        sku_names = {
            str(sku.get("name") or "").casefold()
            for sku in skus or []
            if isinstance(sku, Mapping)
        }
        if (
            str(model.get("name") or "").casefold() == model_name.casefold()
            and str(model.get("version") or "").casefold() == model_version.casefold()
            and str(model.get("format") or "").casefold() == model_format.casefold()
            and (not sku_names or sku_name.casefold() in sku_names)
        ):
            return True
    return False


def model_quota_available(
    cli: AzureCli,
    *,
    location: str,
    model_name: str,
    model_format: str,
    sku_name: str,
    capacity: int,
) -> bool:
    value = cli.json(["cognitiveservices", "usage", "list", "--location", location])
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_quota_catalog",
            "Azure CLI returned an invalid Cognitive Services quota catalog.",
        )

    def normalized(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.casefold())

    namespace = "OpenAI" if model_format.casefold() == "openai" else "AIServices"
    account_quota_found = False
    account_quota_available = False
    model_quota_found = False
    model_quota_available = False
    expected_prefix = f"{namespace}.{sku_name}.".casefold()
    expected_model = normalized(model_name)
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        quota_name = (
            str(name.get("value") or "") if isinstance(name, Mapping) else ""
        )
        try:
            current = float(item.get("currentValue") or 0)
            limit = float(item.get("limit") or 0)
        except (TypeError, ValueError) as error:
            raise OnboardingError(
                "invalid_quota_catalog",
                "Azure quota entry contained a nonnumeric limit.",
            ) from error
        if quota_name.casefold() == f"{namespace}.S0.AccountCount".casefold():
            account_quota_found = True
            account_quota_available = limit - current >= 1
        if quota_name.casefold().startswith(expected_prefix):
            quota_model = quota_name[len(expected_prefix) :]
            if normalized(quota_model) == expected_model:
                model_quota_found = True
                model_quota_available = limit - current >= capacity
    return (
        account_quota_found
        and account_quota_available
        and model_quota_found
        and model_quota_available
    )
