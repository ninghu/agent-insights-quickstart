"""Read-only Azure and Foundry discovery."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .azure_cli import AzureCli
from .errors import OnboardingError
from .models import AzureContext
from .resource_ids import parse_resource_id, require_resource_type
from .validation import validate_project_endpoint

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


def list_application_insights(
    cli: AzureCli,
    subscription_id: str,
    *,
    resource_group: str | None = None,
) -> list[dict[str, str]]:
    arguments = [
        "resource",
        "list",
        "--subscription",
        subscription_id,
        "--resource-type",
        "Microsoft.Insights/components",
    ]
    if resource_group:
        arguments.extend(("--resource-group", resource_group))
    value = cli.json(arguments)
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_application_insights_list",
            "Azure CLI returned an invalid Application Insights list.",
        )
    components: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        resource_id = require_resource_type(
            str(item["id"]),
            "Microsoft.Insights/components",
        )
        components.append(
            {
                "id": resource_id.raw,
                "name": resource_id.name,
                "resource_group": resource_id.resource_group,
                "location": str(item.get("location") or ""),
            }
        )
    return components


def find_project_by_endpoint(cli: AzureCli, project_endpoint: str) -> dict[str, str]:
    endpoint = validate_project_endpoint(project_endpoint)
    parsed_endpoint = urlparse(endpoint)
    account_name = (parsed_endpoint.hostname or "").removesuffix(
        ".services.ai.azure.com"
    )
    project_name = unquote(parsed_endpoint.path.rstrip("/").split("/")[-1])
    account = cli.account_show()
    tenant_id = str(account.get("tenantId") or "")
    subscriptions = [
        item
        for item in list_subscriptions(cli)
        if item["state"].casefold() == "enabled"
        and item["tenant_id"].casefold() == tenant_id.casefold()
    ]
    if not subscriptions:
        raise OnboardingError(
            "no_active_tenant_subscriptions",
            "Azure CLI has no enabled subscriptions in the active tenant.",
        )
    escaped_name = f"{account_name}/{project_name}".replace("'", "''")
    response = cli.rest(
        method="post",
        url=(
            "https://management.azure.com/providers/Microsoft.ResourceGraph/resources"
            "?api-version=2022-10-01"
        ),
        body={
            "subscriptions": [item["id"] for item in subscriptions],
            "query": (
                "Resources "
                "| where type =~ 'microsoft.cognitiveservices/accounts/projects' "
                f"| where name =~ '{escaped_name}' "
                "| project id, name, location, subscriptionId"
            ),
            "options": {"resultFormat": "objectArray"},
        },
    )
    data = response.get("data") if isinstance(response, Mapping) else None
    if not isinstance(data, list):
        raise OnboardingError(
            "invalid_resource_graph_response",
            "Azure Resource Graph returned an invalid project result.",
        )
    matches = [item for item in data if isinstance(item, Mapping)]
    if not matches:
        raise OnboardingError(
            "project_not_found_in_active_tenant",
            "No Foundry project matching the endpoint was found in the active tenant.",
            {
                "active_tenant_id": tenant_id,
                "searched_subscription_count": len(subscriptions),
            },
        )
    if len(matches) != 1:
        raise OnboardingError(
            "ambiguous_project_endpoint",
            "Multiple ARM projects matched the Foundry project endpoint.",
            {"project_ids": [str(item.get("id") or "") for item in matches]},
        )
    match = matches[0]
    resource_id = require_resource_type(
        str(match.get("id") or ""),
        "Microsoft.CognitiveServices/accounts/projects",
    )
    validate_project_endpoint(endpoint, resource_id.name, resource_id.names[0])
    subscription_id = str(match.get("subscriptionId") or resource_id.subscription_id)
    if subscription_id.casefold() != resource_id.subscription_id.casefold():
        raise OnboardingError(
            "resource_graph_subscription_mismatch",
            "Azure Resource Graph returned conflicting subscription identifiers.",
        )
    return {
        "project_resource_id": resource_id.raw,
        "project_endpoint": endpoint,
        "subscription_id": resource_id.subscription_id,
        "location": str(match.get("location") or ""),
        "account_name": resource_id.names[0],
        "project_name": resource_id.name,
    }


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
        lifecycle = str(model.get("lifecycleStatus") or "").casefold()
        if lifecycle in {"deprecated", "retired"}:
            continue
        deprecation = model.get("deprecation")
        inference_end = (
            str(deprecation.get("inference") or "")
            if isinstance(deprecation, Mapping)
            else ""
        )
        if inference_end:
            try:
                if datetime.fromisoformat(
                    inference_end.replace("Z", "+00:00")
                ) <= datetime.now(UTC):
                    continue
            except ValueError as error:
                raise OnboardingError(
                    "invalid_model_catalog",
                    "Model deprecation metadata contained an invalid timestamp.",
                ) from error
        capabilities = model.get("capabilities")
        if isinstance(capabilities, Mapping) and not (
            str(capabilities.get("chatCompletion") or "").casefold() == "true"
            and str(capabilities.get("responses") or "").casefold() == "true"
        ):
            continue
        skus = model.get("skus") or item.get("skus")
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


def list_recommended_insight_models(
    cli: AzureCli,
    *,
    location: str,
    minimum_capacity: int = 30,
) -> list[dict[str, Any]]:
    catalog = cli.json(["cognitiveservices", "model", "list", "--location", location])
    usage = cli.json(["cognitiveservices", "usage", "list", "--location", location])
    if not isinstance(catalog, list) or not isinstance(usage, list):
        raise OnboardingError(
            "invalid_model_catalog",
            "Azure CLI returned invalid model or quota data.",
        )
    headroom: dict[str, float] = {}
    for item in usage:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        usage_name = (
            str(name.get("value") or "") if isinstance(name, Mapping) else ""
        )
        try:
            headroom[usage_name] = float(item.get("limit") or 0) - float(
                item.get("currentValue") or 0
            )
        except (TypeError, ValueError) as error:
            raise OnboardingError(
                "invalid_quota_catalog",
                "Azure quota entry contained a nonnumeric limit.",
            ) from error
    if headroom.get("OpenAI.S0.AccountCount", 0) < 1:
        return []

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in catalog:
        if not isinstance(item, Mapping):
            continue
        model = item.get("model")
        if not isinstance(model, Mapping):
            continue
        name = str(model.get("name") or "")
        version = str(model.get("version") or "")
        if not name.casefold().startswith("gpt-5") or not version:
            continue
        if str(model.get("lifecycleStatus") or "").casefold() in {
            "deprecated",
            "retired",
        }:
            continue
        deprecation = model.get("deprecation")
        inference_end = (
            str(deprecation.get("inference") or "")
            if isinstance(deprecation, Mapping)
            else ""
        )
        if inference_end:
            try:
                if datetime.fromisoformat(
                    inference_end.replace("Z", "+00:00")
                ) <= datetime.now(UTC):
                    continue
            except ValueError as error:
                raise OnboardingError(
                    "invalid_model_catalog",
                    "Model deprecation metadata contained an invalid timestamp.",
                ) from error
        capabilities = model.get("capabilities")
        if not isinstance(capabilities, Mapping) or not (
            str(capabilities.get("chatCompletion") or "").casefold() == "true"
            and str(capabilities.get("responses") or "").casefold() == "true"
        ):
            continue
        for sku in model.get("skus") or []:
            if (
                not isinstance(sku, Mapping)
                or str(sku.get("name") or "").casefold() != "globalstandard"
            ):
                continue
            usage_name = str(sku.get("usageName") or "")
            available = headroom.get(usage_name, 0)
            if available < minimum_capacity:
                continue
            quality_tier = (
                "preferred_customer_candidate"
                if name.casefold() == "gpt-5.6-terra"
                else "service_regression_baseline"
                if name.casefold() == "gpt-5.4"
                else "gpt5_plus_candidate"
            )
            priority = (
                0
                if quality_tier == "preferred_customer_candidate"
                else 1
                if quality_tier == "service_regression_baseline"
                else 2
            )
            candidates[(name.casefold(), version.casefold())] = {
                "name": name,
                "version": version,
                "format": str(model.get("format") or "OpenAI"),
                "sku_name": str(sku.get("name") or ""),
                "recommended_capacity": minimum_capacity,
                "quota_headroom": available,
                "lifecycle_status": str(model.get("lifecycleStatus") or ""),
                "quality_tier": quality_tier,
                "_priority": priority,
            }
    result = sorted(
        candidates.values(),
        key=lambda item: (
            int(item["_priority"]),
            str(item["name"]).casefold(),
            str(item["version"]).casefold(),
        ),
    )
    for item in result:
        item.pop("_priority", None)
    return result


def list_model_deployments(
    cli: AzureCli,
    project_resource_id: str,
) -> list[dict[str, Any]]:
    project = require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    account = project.parent()
    value = cli.json(
        [
            "cognitiveservices",
            "account",
            "deployment",
            "list",
            "--resource-group",
            account.resource_group,
            "--name",
            account.name,
        ]
    )
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_deployment_list",
            "Azure CLI returned an invalid model deployment list.",
        )
    deployments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        properties = item.get("properties")
        model = properties.get("model") if isinstance(properties, Mapping) else None
        sku = item.get("sku")
        if not isinstance(model, Mapping):
            continue
        model_name = str(model.get("name") or "")
        deployments.append(
            {
                "deployment_name": str(item.get("name") or ""),
                "model_name": model_name,
                "model_version": str(model.get("version") or ""),
                "model_format": str(model.get("format") or ""),
                "sku_name": (
                    str(sku.get("name") or "") if isinstance(sku, Mapping) else ""
                ),
                "capacity": (
                    int(sku.get("capacity") or 0) if isinstance(sku, Mapping) else 0
                ),
                "gpt5_plus": model_name.casefold().startswith("gpt-5"),
            }
        )
    return sorted(
        deployments,
        key=lambda item: (
            not bool(item["gpt5_plus"]),
            str(item["deployment_name"]).casefold(),
        ),
    )


def model_deployment_command(
    project_resource_id: str,
    *,
    deployment_name: str,
    model_name: str,
    model_version: str,
    model_format: str,
    sku_name: str,
    capacity: int,
) -> list[str]:
    account = require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    ).parent()
    return [
        "az",
        "cognitiveservices",
        "account",
        "deployment",
        "create",
        "--subscription",
        account.subscription_id,
        "--resource-group",
        account.resource_group,
        "--name",
        account.name,
        "--deployment-name",
        deployment_name,
        "--model-name",
        model_name,
        "--model-version",
        model_version,
        "--model-format",
        model_format,
        "--sku-name",
        sku_name,
        "--sku-capacity",
        str(capacity),
    ]
