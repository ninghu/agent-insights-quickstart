"""Scratch provisioning and exact-ownership cleanup."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .azure_cli import AzureCli
from .discovery import (
    account_id_from_project,
    derive_project_endpoint,
    get_application_insights,
    get_project,
    linked_workspace_id,
    list_app_insights_connections,
    list_connections,
)
from .errors import OnboardingError
from .models import AzureContext, OnboardingConfig, ProjectResources
from .resource_ids import require_resource_type
from .validation import normalize_name, require_owned_tags, validate_project_endpoint

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_MAIN_BICEP = _SKILL_ROOT / "assets" / "infra" / "main.bicep"
_EXISTING_CONNECTIONS_BICEP = (
    _SKILL_ROOT / "assets" / "infra" / "existing-connections.bicep"
)


def resource_group_name(config: OnboardingConfig, run_id: str) -> str:
    prefix = normalize_name(config.name_prefix, max_length=32)
    return f"rg-{prefix}-{run_id}"


def _tags(run_id: str, context: AzureContext) -> dict[str, str]:
    return {
        "created-by": "agent-insights-quickstart",
        "run-id": run_id,
        "owner-object-id": context.user_object_id,
    }


def _ensure_resource_group(
    cli: AzureCli,
    *,
    name: str,
    location: str,
    run_id: str,
    context: AzureContext,
) -> Mapping[str, Any]:
    exists = cli.json(["group", "exists", "--name", name])
    if exists is True:
        group = cli.json(["group", "show", "--name", name])
        if not isinstance(group, Mapping):
            raise OnboardingError(
                "invalid_resource_group",
                "Existing resource group response was invalid.",
            )
        require_owned_tags(
            group.get("tags"),
            run_id=run_id,
            owner_object_id=context.user_object_id,
        )
        return group
    if exists is not False:
        raise OnboardingError(
            "invalid_resource_group_check",
            "Azure returned an invalid resource-group existence result.",
        )
    group = cli.json(
        [
            "group",
            "create",
            "--name",
            name,
            "--location",
            location,
            "--tags",
            *[f"{key}={value}" for key, value in _tags(run_id, context).items()],
        ]
    )
    if not isinstance(group, Mapping):
        raise OnboardingError(
            "invalid_resource_group",
            "Resource-group creation response was invalid.",
        )
    require_owned_tags(
        group.get("tags"),
        run_id=run_id,
        owner_object_id=context.user_object_id,
    )
    return group


def _deployment_outputs(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "invalid_deployment",
            "ARM deployment response was invalid.",
        )
    properties = value.get("properties")
    outputs = properties.get("outputs") if isinstance(properties, Mapping) else None
    if not isinstance(outputs, Mapping):
        raise OnboardingError(
            "missing_deployment_outputs",
            "ARM deployment returned no outputs.",
        )
    result: dict[str, Any] = {}
    for name, output in outputs.items():
        if not isinstance(output, Mapping) or "value" not in output:
            raise OnboardingError(
                "invalid_deployment_output",
                f"ARM deployment output '{name}' was invalid.",
            )
        result[str(name)] = output["value"]
    return result


def provision_scratch(
    cli: AzureCli,
    *,
    config: OnboardingConfig,
    context: AzureContext,
    run_id: str,
) -> ProjectResources:
    if not config.location or not config.agent_type:
        raise OnboardingError(
            "incomplete_scratch_configuration",
            "Scratch mode requires location and agent type.",
        )
    group_name = resource_group_name(config, run_id)
    group = _ensure_resource_group(
        cli,
        name=group_name,
        location=config.location,
        run_id=run_id,
        context=context,
    )
    name_prefix = normalize_name(config.name_prefix, max_length=12)
    model_deployment_name = config.model_deployment_name or normalize_name(
        config.model_name
    )
    deployment = cli.json(
        [
            "deployment",
            "group",
            "create",
            "--resource-group",
            group_name,
            "--name",
            f"agent-insights-{run_id}",
            "--template-file",
            str(_MAIN_BICEP),
            "--parameters",
            f"namePrefix={name_prefix}",
            f"nameSuffix={run_id}",
            f"location={config.location}",
            f"initiatingUserObjectId={context.user_object_id}",
            f"agentType={config.agent_type}",
            f"modelName={config.model_name}",
            f"modelVersion={config.model_version}",
            f"modelFormat={config.model_format}",
            f"modelDeploymentName={model_deployment_name}",
            f"modelSkuName={config.model_sku}",
            f"modelSkuCapacity={config.model_capacity}",
            f"grantPrivilegedMonitoringDataReader={str(config.protected_trace_content).lower()}",
            f"tags={json.dumps(_tags(run_id, context), separators=(',', ':'))}",
        ],
        timeout=3600,
    )
    outputs = _deployment_outputs(deployment)
    required = {
        "foundryAccountId",
        "foundryProjectId",
        "foundryProjectEndpoint",
        "foundryProjectPrincipalId",
        "logAnalyticsWorkspaceId",
        "applicationInsightsId",
        "modelDeploymentName",
    }
    missing = sorted(required - outputs.keys())
    if missing:
        raise OnboardingError(
            "missing_deployment_outputs",
            "Scratch deployment omitted required outputs.",
            {"missing": missing},
        )
    project_id = str(outputs["foundryProjectId"])
    project_resource = require_resource_type(
        project_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    endpoint = str(outputs["foundryProjectEndpoint"] or "")
    if not endpoint:
        endpoint = derive_project_endpoint(project_id)
    return ProjectResources(
        project_resource_id=project_id,
        project_endpoint=validate_project_endpoint(
            endpoint,
            project_resource.name,
            project_resource.names[0],
        ),
        project_principal_id=str(outputs["foundryProjectPrincipalId"]),
        foundry_account_resource_id=require_resource_type(
            str(outputs["foundryAccountId"]),
            "Microsoft.CognitiveServices/accounts",
        ).raw,
        application_insights_resource_id=require_resource_type(
            str(outputs["applicationInsightsId"]),
            "Microsoft.Insights/components",
        ).raw,
        log_analytics_workspace_resource_id=require_resource_type(
            str(outputs["logAnalyticsWorkspaceId"]),
            "Microsoft.OperationalInsights/workspaces",
        ).raw,
        model_deployment_name=str(outputs["modelDeploymentName"]),
        resource_group_id=str(group.get("id") or ""),
    )


def resolve_existing(
    cli: AzureCli,
    *,
    config: OnboardingConfig,
) -> ProjectResources:
    if (
        not config.project_resource_id
        or not config.agent_name
        or not config.model_deployment_name
    ):
        raise OnboardingError(
            "incomplete_existing_configuration",
            "Existing mode requires project, agent, and model deployment.",
        )
    project_id = require_resource_type(
        config.project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    if project_id.subscription_id.casefold() != config.subscription_id.casefold():
        raise OnboardingError(
            "project_subscription_mismatch",
            "Selected project does not belong to the selected subscription.",
        )
    project = get_project(cli, project_id.raw)
    identity = project.get("identity")
    principal_id = (
        str(identity.get("principalId") or "")
        if isinstance(identity, Mapping)
        else ""
    )
    connections = list_app_insights_connections(cli, project_id.raw)
    if len(connections) > 1:
        raise OnboardingError(
            "ambiguous_app_insights_connection",
            "The project has multiple Application Insights connections.",
            {"connection_ids": [item["id"] for item in connections]},
        )
    connected_id = connections[0]["resource_id"] if connections else ""
    if connections and not connected_id:
        raise OnboardingError(
            "invalid_app_insights_connection",
            "The existing Application Insights connection has no resource ID metadata.",
        )
    selected_id = config.application_insights_resource_id or connected_id
    if not selected_id:
        raise OnboardingError(
            "missing_app_insights_connection",
            "Select an Application Insights component so the project connection can be created.",
        )
    selected_id = require_resource_type(
        selected_id,
        "Microsoft.Insights/components",
    ).raw
    if connected_id and connected_id.rstrip("/").casefold() != selected_id.casefold():
        raise OnboardingError(
            "app_insights_connection_mismatch",
            "Configured Application Insights differs from the existing project connection.",
        )
    app_insights = get_application_insights(cli, selected_id)
    endpoint = config.project_endpoint or derive_project_endpoint(project_id.raw)
    return ProjectResources(
        project_resource_id=project_id.raw,
        project_endpoint=validate_project_endpoint(
            endpoint,
            project_id.name,
            project_id.names[0],
        ),
        project_principal_id=principal_id,
        foundry_account_resource_id=account_id_from_project(project_id.raw),
        application_insights_resource_id=selected_id,
        log_analytics_workspace_resource_id=linked_workspace_id(app_insights),
        model_deployment_name=config.model_deployment_name,
    )


def ensure_project_identity(
    cli: AzureCli,
    *,
    project_resource_id: str,
) -> str:
    project = get_project(cli, project_resource_id)
    identity = project.get("identity")
    principal_id = (
        str(identity.get("principalId") or "")
        if isinstance(identity, Mapping)
        else ""
    )
    if principal_id:
        return principal_id
    updated = cli.json(
        [
            "resource",
            "update",
            "--ids",
            project_resource_id,
            "--api-version",
            "2025-06-01",
            "--set",
            "identity.type=SystemAssigned",
        ]
    )
    if not isinstance(updated, Mapping):
        raise OnboardingError(
            "project_identity_failed",
            "Foundry project identity update response was invalid.",
        )
    identity = updated.get("identity")
    principal_id = (
        str(identity.get("principalId") or "")
        if isinstance(identity, Mapping)
        else ""
    )
    if not principal_id:
        raise OnboardingError(
            "project_identity_failed",
            "Foundry project identity did not return a principal ID.",
        )
    return principal_id


def ensure_existing_connections(
    cli: AzureCli,
    *,
    project_resource_id: str,
    application_insights_resource_id: str,
    location: str,
    run_id: str,
) -> tuple[str, ...]:
    project_connections = list_app_insights_connections(cli, project_resource_id)
    if len(project_connections) == 1:
        return ()
    if project_connections:
        raise OnboardingError(
            "ambiguous_app_insights_connection",
            "The project has multiple Application Insights connections.",
        )
    connection_plan = plan_existing_connections(
        cli,
        project_resource_id=project_resource_id,
        application_insights_resource_id=application_insights_resource_id,
    )
    project = require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    app_insights = require_resource_type(
        application_insights_resource_id,
        "Microsoft.Insights/components",
    )
    account_name, project_name = project.names
    deployment = cli.json(
        [
            "deployment",
            "sub",
            "create",
            "--location",
            location,
            "--name",
            f"agent-insights-connection-{run_id}",
            "--template-file",
            str(_EXISTING_CONNECTIONS_BICEP),
            "--parameters",
            f"foundryResourceGroup={project.resource_group}",
            f"foundryAccountName={account_name}",
            f"foundryProjectName={project_name}",
            f"applicationInsightsSubscriptionId={app_insights.subscription_id}",
            f"applicationInsightsResourceGroup={app_insights.resource_group}",
            f"applicationInsightsName={app_insights.name}",
            f"projectConnectionName={connection_plan['project_connection_name']}",
        ],
        timeout=900,
    )
    outputs = _deployment_outputs(deployment)
    ids = tuple(
        str(outputs[name])
        for name in ("projectConnectionId",)
        if outputs.get(name)
    )
    if len(ids) != 1:
        raise OnboardingError(
            "connection_create_failed",
            "Application Insights connection deployment returned incomplete outputs.",
        )
    return ids


def plan_existing_connections(
    cli: AzureCli,
    *,
    project_resource_id: str,
    application_insights_resource_id: str,
) -> dict[str, str | bool]:
    require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    app_insights = require_resource_type(
        application_insights_resource_id,
        "Microsoft.Insights/components",
    )
    connection_name = "agent-insights-" + hashlib.sha256(
        app_insights.raw.casefold().encode()
    ).hexdigest()[:10]
    for item in list_connections(cli, project_resource_id):
        if item["name"].casefold() == connection_name.casefold():
            raise OnboardingError(
                "project_connection_name_conflict",
                "The project connection name is already used by another connection.",
            )
    return {
        "project_connection_name": connection_name,
    }


def cleanup_scratch(
    cli: AzureCli,
    *,
    resource_group_id: str,
    run_id: str,
    owner_object_id: str,
) -> None:
    group = require_resource_type(
        # Parse this as a pseudo-resource by appending a harmless provider resource.
        f"{resource_group_id}/providers/Microsoft.Resources/deployments/ownership-check",
        "Microsoft.Resources/deployments",
    )
    value = cli.json(["group", "show", "--name", group.resource_group])
    if not isinstance(value, Mapping):
        raise OnboardingError(
            "invalid_resource_group",
            "Resource-group cleanup response was invalid.",
        )
    if str(value.get("id") or "").rstrip("/").casefold() != resource_group_id.rstrip(
        "/"
    ).casefold():
        raise OnboardingError(
            "ownership_mismatch",
            "Cleanup resolved a different resource group ID.",
        )
    require_owned_tags(
        value.get("tags"),
        run_id=run_id,
        owner_object_id=owner_object_id,
    )
    cli.run(["group", "delete", "--name", group.resource_group, "--yes"], timeout=3600)
