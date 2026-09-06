"""End-to-end onboarding state machine."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, cast

from .agents import agent_name as sample_agent_name
from .agents import (
    create_sample_agent,
    delete_owned_agent,
    find_owned_agent,
    project_client,
    validate_existing_agent,
    verify_owned_agent,
)
from .azure_cli import AzureCli, is_resource_not_found
from .discovery import (
    check_azure_cli_version,
    get_project,
    list_app_insights_connections,
    model_is_available,
    model_quota_available,
    permissions_at_scope,
    provider_states,
    select_context,
    subscription_permissions,
)
from .errors import OnboardingError
from .ingestion import (
    can_query_agent_traces,
    require_recent_agent_roots,
    wait_for_ingestion,
)
from .insights_api import AgentInsightsClient, normalize_run_trigger
from .models import (
    AgentDeployment,
    AzureContext,
    MonitorOutcome,
    Mutation,
    OnboardingConfig,
    OnboardingPlan,
    ProjectResources,
    TrafficOutcome,
)
from .permissions import (
    COGNITIVE_SERVICES_OPENAI_USER,
    FOUNDRY_PROJECT_MANAGER,
    FOUNDRY_USER,
    MONITORING_READER,
    PRIVILEGED_MONITORING_DATA_READER,
    PrincipalType,
    RequiredAssignment,
    create_assignment,
    missing_assignments,
    require_actions,
    required_assignments,
)
from .portal import agent_insights_url, foundry_project_url
from .provisioning import (
    cleanup_scratch,
    ensure_existing_connections,
    ensure_project_identity,
    plan_existing_connections,
    provision_scratch,
    resolve_existing,
    resource_group_name,
)
from .receipts import (
    ProvisioningJournal,
    ResourceObserver,
    read_json,
    verify_plan_payload,
    write_json_atomic,
)
from .resource_ids import parse_resource_id
from .traffic import generate_sample_traffic
from .validation import require_owned_tags, validate_plan_context, validate_run_id

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_FEEDBACK_URL = (
    "https://msdata.visualstudio.com/Vienna/_workitems/create/Bug"
    "?templateId=6d5d4dfe-fd55-45f3-b9c9-f7cc2b0e1835"
    "&ownerId=5d069bfc-f7ae-4d93-bee7-c94d439a26a7"
)


def _find_workspace_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


_REPO_ROOT = _find_workspace_root()
_RUNS_ROOT = _REPO_ROOT / ".agent-insights" / "runs"
_PROVIDERS = (
    "Microsoft.CognitiveServices",
    "Microsoft.Insights",
    "Microsoft.OperationalInsights",
)
_SCRATCH_ACTIONS = (
    "Microsoft.Resources/subscriptions/resourceGroups/write",
    "Microsoft.Resources/subscriptions/resourceGroups/delete",
    "Microsoft.CognitiveServices/accounts/write",
    "Microsoft.CognitiveServices/accounts/projects/write",
    "Microsoft.CognitiveServices/accounts/deployments/write",
    "Microsoft.CognitiveServices/accounts/connections/write",
    "Microsoft.CognitiveServices/accounts/projects/connections/write",
    "Microsoft.Insights/components/write",
    "Microsoft.OperationalInsights/workspaces/write",
    "Microsoft.Authorization/roleAssignments/write",
    "Microsoft.Authorization/roleAssignments/delete",
)


def _check_python_and_packages() -> dict[str, str]:
    python_version = tuple(int(value) for value in platform.python_version_tuple()[:2])
    if python_version < (3, 13):
        raise OnboardingError(
            "python_too_old",
            "Python 3.13 or newer is required.",
            {"found": ".".join(str(value) for value in sys.version_info[:3])},
        )
    expected = {"azure-ai-projects": "2.3.0"}
    versions: dict[str, str] = {}
    for distribution, required in expected.items():
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise OnboardingError(
                "dependency_missing",
                f"Required Python package is missing: {distribution}=={required}.",
            ) from error
        if installed != required:
            raise OnboardingError(
                "dependency_version_mismatch",
                f"{distribution} must be pinned to {required}.",
                {"found": installed},
            )
        versions[distribution] = installed
    return versions


def _require_registered_providers(cli: AzureCli) -> dict[str, str]:
    states = provider_states(cli, _PROVIDERS)
    missing = [
        namespace
        for namespace, state in states.items()
        if state.casefold() != "registered"
    ]
    if missing:
        raise OnboardingError(
            "provider_not_registered",
            "Required Azure resource providers must be registered before onboarding.",
            {"providers": missing},
        )
    return states


def _credential(context: AzureContext) -> Any:
    identity = importlib.import_module("azure.identity")
    return identity.AzureCliCredential(tenant_id=context.tenant_id)


def _validate_existing_model(cli: AzureCli, resources: ProjectResources) -> None:
    account = parse_resource_id(resources.foundry_account_resource_id)
    value = cli.json(
        [
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--resource-group",
            account.resource_group,
            "--name",
            account.name,
            "--deployment-name",
            resources.model_deployment_name,
        ]
    )
    if not isinstance(value, dict) or not value.get("name"):
        raise OnboardingError(
            "model_unavailable",
            "Selected model deployment was not found on the project's Foundry account. "
            "An Entra-authenticated model connected from another account requires "
            "separate permission handling.",
        )


def _require_assignment_write(
    cli: AzureCli,
    scopes: set[str],
    assignments: Sequence[RequiredAssignment] = (),
) -> None:
    handoff_assignments = [
        {
            "principal_id": assignment.principal_id,
            "principal_type": assignment.principal_type,
            "role_name": assignment.role.name,
            "role_definition_id": assignment.role.definition_id,
            "scope": assignment.scope,
            "assignment_id": assignment.assignment_id,
            "command": [
                "az",
                "role",
                "assignment",
                "create",
                "--assignee-object-id",
                assignment.principal_id,
                "--assignee-principal-type",
                assignment.principal_type,
                "--role",
                assignment.role.definition_id,
                "--scope",
                assignment.scope,
                "--name",
                assignment.assignment_id,
            ],
        }
        for assignment in assignments
    ]
    for scope in sorted(scopes):
        try:
            require_actions(
                permissions_at_scope(cli, scope),
                [
                    "Microsoft.Authorization/roleAssignments/write",
                    "Microsoft.Authorization/roleAssignments/delete",
                ],
                scope=scope,
            )
        except OnboardingError as error:
            raise OnboardingError(
                "insufficient_preflight_permission",
                "An Azure administrator must complete the RBAC handoff before onboarding.",
                {
                    **error.details,
                    "admin_handoff": {
                        "instructions": (
                            "Ask an Azure administrator to apply the exact role "
                            "assignments, then rerun the same doctor command. Do not "
                            "continue based only on confirmation."
                        ),
                        "role_assignments": handoff_assignments,
                        "verification": "Rerun doctor and require status=ready.",
                    },
                },
            ) from error


def _existing_caller_capabilities(
    *,
    context: AzureContext,
    resources: ProjectResources,
) -> tuple[bool, bool]:
    credential = _credential(context)
    with AgentInsightsClient(
        project_endpoint=resources.project_endpoint,
        credential=credential,
    ) as client:
        foundry_authorized = bool(client.probe().get("authorized"))
    monitoring_authorized = can_query_agent_traces(
        credential=credential,
        application_insights_resource_id=
            resources.application_insights_resource_id,
    )
    return foundry_authorized, monitoring_authorized


def _filter_existing_caller_assignments(
    assignments: list[RequiredAssignment],
    *,
    context: AzureContext,
    foundry_authorized: bool,
    monitoring_authorized: bool,
) -> list[RequiredAssignment]:
    filtered: list[RequiredAssignment] = []
    for assignment in assignments:
        if assignment.principal_id.casefold() != context.user_object_id.casefold():
            filtered.append(assignment)
            continue
        if foundry_authorized and assignment.role == FOUNDRY_USER:
            continue
        if monitoring_authorized and assignment.role == MONITORING_READER:
            continue
        filtered.append(assignment)
    return filtered


def _validate_agent_selection(config: OnboardingConfig) -> None:
    if config.profile not in {"standard", "bug-bash"}:
        raise OnboardingError("invalid_profile", "Unsupported workflow profile.")
    if config.profile == "bug-bash":
        if config.enable_existing_monitor:
            raise OnboardingError(
                "bug_bash_scheduling_unsupported",
                "Bug bash uses a single manual run; scheduled generation is not allowed.",
            )
        if not config.creates_sample_agent or config.agent_name or config.invoke_existing_agent:
            raise OnboardingError(
                "bug_bash_sample_required",
                "Bug bash requires a new owned sample Agent, not an existing customer Agent.",
            )
    if config.mode == "scratch" and config.create_sample_agent:
        raise OnboardingError(
            "invalid_agent_selection",
            "Scratch mode creates a sample Agent automatically.",
        )
    if config.mode != "existing" or not config.create_sample_agent:
        if config.mode == "existing" and config.invoke_existing_agent:
            raise OnboardingError(
                "existing_agent_invocation_unsupported",
                "The quickstart does not generate traffic for an existing customer Agent. "
                "Run normal application traffic and rerun doctor.",
            )
        return
    if config.agent_name:
        raise OnboardingError(
            "conflicting_agent_selection",
            "Choose either a new sample Agent or an existing Agent, not both.",
        )


def doctor(config: OnboardingConfig, cli: AzureCli | None = None) -> dict[str, Any]:
    _validate_agent_selection(config)
    selected_cli = cli or AzureCli()
    package_versions = _check_python_and_packages()
    cli_version = check_azure_cli_version(selected_cli)
    context = select_context(selected_cli, config.subscription_id)
    providers = _require_registered_providers(selected_cli)
    result: dict[str, Any] = {
        "status": "ready",
        "mode": config.mode,
        "python": ".".join(str(value) for value in sys.version_info[:3]),
        "packages": package_versions,
        "azure_cli": cli_version,
        "azure_context": asdict(context),
        "providers": providers,
    }
    if config.mode == "scratch":
        if not config.location or not config.agent_type:
            raise OnboardingError(
                "incomplete_scratch_configuration",
                "Scratch mode requires --location and --agent-type.",
            )
        require_actions(
            subscription_permissions(selected_cli, config.subscription_id),
            _SCRATCH_ACTIONS,
            scope=f"/subscriptions/{config.subscription_id}",
        )
        if not model_is_available(
            selected_cli,
            location=config.location,
            model_name=config.model_name,
            model_version=config.model_version,
            model_format=config.model_format,
            sku_name=config.model_sku,
        ):
            raise OnboardingError(
                "model_unavailable",
                "Selected model/version/SKU is not available in the target region.",
                {
                    "location": config.location,
                    "model": config.model_name,
                    "version": config.model_version,
                    "sku": config.model_sku,
                },
            )
        if not model_quota_available(
            selected_cli,
            location=config.location,
            model_name=config.model_name,
            model_format=config.model_format,
            sku_name=config.model_sku,
            capacity=config.model_capacity,
        ):
            raise OnboardingError(
                "model_quota_unavailable",
                "The target region lacks free account or model capacity for this quickstart.",
                {
                    "location": config.location,
                    "model": config.model_name,
                    "sku": config.model_sku,
                    "required_capacity": config.model_capacity,
                },
            )
        result["scratch"] = {
            "resource_group": resource_group_name(config, "000000000000"),
            "agent_type": config.agent_type,
            "model_deployment_name": config.model_deployment_name
            or config.model_name.replace(".", "-"),
        }
        return result

    if not config.agent_type:
        raise OnboardingError(
            "incomplete_existing_configuration",
            "Existing mode requires --agent-type to select the correct permission policy.",
        )
    resources = resolve_existing(selected_cli, config=config)
    project = get_project(selected_cli, resources.project_resource_id)
    project_location = str(project.get("location") or "")
    if not project_location:
        raise OnboardingError(
            "invalid_project",
            "Foundry project has no Azure location.",
        )
    connections = list_app_insights_connections(
        selected_cli,
        resources.project_resource_id,
    )
    project_mi_execution = config.scheduling_enabled
    requires_identity = config.project_mi_telemetry_required
    if requires_identity and not resources.project_principal_id:
        require_actions(
            permissions_at_scope(selected_cli, resources.project_resource_id),
            ["Microsoft.CognitiveServices/accounts/projects/write"],
            scope=resources.project_resource_id,
        )
    if not connections:
        plan_existing_connections(
            selected_cli,
            project_resource_id=resources.project_resource_id,
            application_insights_resource_id=
                resources.application_insights_resource_id,
        )
        require_actions(
            permissions_at_scope(selected_cli, resources.project_resource_id),
            ["Microsoft.CognitiveServices/accounts/projects/connections/write"],
            scope=resources.project_resource_id,
        )
    _validate_existing_model(selected_cli, resources)
    foundry_authorized, monitoring_authorized = _existing_caller_capabilities(
        context=context,
        resources=resources,
    )
    recent_trace_count: int | None = None
    if foundry_authorized and not config.create_sample_agent:
        deployment = validate_existing_agent(
            project_client(resources.project_endpoint, context.tenant_id),
            name=config.agent_name or "",
        )
        if deployment.kind != config.agent_type:
            raise OnboardingError(
                "agent_type_mismatch",
                "Existing Agent kind differs from the selected permission policy.",
                {"selected": config.agent_type, "actual": deployment.kind},
            )
        if monitoring_authorized:
            recent_trace_count = len(
                require_recent_agent_roots(
                    credential=_credential(context),
                    application_insights_resource_id=
                        resources.application_insights_resource_id,
                    deployment=deployment,
                    lookback_hours=config.lookback_hours,
                )
            )
    if resources.project_principal_id or not requires_identity:
        assignments = required_assignments(
            current_user_id=context.user_object_id,
            project_principal_id=resources.project_principal_id,
            foundry_account_id=resources.foundry_account_resource_id,
            project_id=resources.project_resource_id,
            application_insights_id=resources.application_insights_resource_id,
            workspace_id=resources.log_analytics_workspace_resource_id,
            agent_type=config.agent_type,
            protected_trace_content=config.protected_trace_content,
            project_mi_execution=project_mi_execution,
            project_mi_telemetry=config.project_mi_telemetry_required,
            manage_hosted_agent=config.create_sample_agent,
        )
        assignments = _filter_existing_caller_assignments(
            assignments,
            context=context,
            foundry_authorized=foundry_authorized,
            monitoring_authorized=monitoring_authorized,
        )
        missing_required_assignments = missing_assignments(selected_cli, assignments)
        scopes_requiring_assignments = {
            item.scope for item in missing_required_assignments
        }
    else:
        missing_required_assignments = []
        scopes_requiring_assignments = {
            mutation.target
            for mutation in _unresolved_identity_role_mutations(
                config=config,
                context=context,
                resources=resources,
            )
        }
    _require_assignment_write(
        selected_cli,
        scopes_requiring_assignments,
        missing_required_assignments,
    )
    result["existing"] = {
        "resources": asdict(resources),
        "project_location": project_location,
        "connection_count": len(connections),
        "feature": {
            "reachable": True,
            "authorized": foundry_authorized,
        },
        "caller_monitoring_authorized": monitoring_authorized,
        "trace_check": (
            {
                "status": "ready",
                "observed_agent_roots": recent_trace_count,
                "lookback_hours": config.lookback_hours,
            }
            if recent_trace_count is not None
            else {
                "status": "not_applicable"
                if config.create_sample_agent
                else "deferred_until_caller_access_is_ready"
            }
        ),
    }
    return result


def _role_mutations(
    cli: AzureCli,
    assignments: list[RequiredAssignment],
) -> list[Mutation]:
    return [
        Mutation(
            "create_role_assignment",
            item.scope,
            {
                "principal_id": item.principal_id,
                "principal_type": item.principal_type,
                "role_definition_id": item.role.definition_id,
                "role_name": item.role.name,
                "assignment_id": item.assignment_id,
            },
        )
        for item in missing_assignments(cli, assignments)
    ]


def _unresolved_identity_role_mutations(
    *,
    config: OnboardingConfig,
    context: AzureContext,
    resources: ProjectResources,
) -> list[Mutation]:
    if not config.agent_type:
        raise OnboardingError("missing_agent_type", "Agent type is required.")
    user_role = (
        FOUNDRY_PROJECT_MANAGER
        if config.agent_type == "hosted" and config.creates_sample_agent
        else FOUNDRY_USER
    )
    planned = [
        (
            "project_system_identity",
            MONITORING_READER,
            resources.application_insights_resource_id,
        ),
        (context.user_object_id, user_role, resources.project_resource_id),
        (
            context.user_object_id,
            MONITORING_READER,
            resources.application_insights_resource_id,
        ),
    ]
    if config.scheduling_enabled:
        planned.insert(0, (
            "project_system_identity", FOUNDRY_USER, resources.foundry_account_resource_id,
        ))
    if config.protected_trace_content:
        planned.extend(
            (
                (
                    "project_system_identity",
                    PRIVILEGED_MONITORING_DATA_READER,
                    resources.log_analytics_workspace_resource_id,
                ),
                (
                    context.user_object_id,
                    PRIVILEGED_MONITORING_DATA_READER,
                    resources.log_analytics_workspace_resource_id,
                ),
            )
        )
    return [
        Mutation(
            "ensure_role_assignment_after_identity",
            scope,
            {
                "principal": principal,
                "role_definition_id": role.definition_id,
                "role_name": role.name,
            },
        )
        for principal, role, scope in planned
    ]


def build_plan(
    config: OnboardingConfig,
    *,
    context: AzureContext,
    run_id: str,
    cli: AzureCli,
) -> OnboardingPlan:
    validate_run_id(run_id)
    _validate_agent_selection(config)
    creates_sample_agent = config.creates_sample_agent
    target_agent_name = (
        sample_agent_name(run_id, config.agent_type)
        if creates_sample_agent and config.agent_type
        else config.agent_name or ""
    )
    mutations: list[Mutation] = []
    if config.mode == "scratch":
        mutations.extend(
            (
                Mutation(
                    "create_resource_group",
                    resource_group_name(config, run_id),
                    {"location": config.location},
                ),
                Mutation(
                    "deploy_scratch_environment",
                    resource_group_name(config, run_id),
                    {
                        "agent_type": config.agent_type,
                        "model": config.model_name,
                        "model_version": config.model_version,
                        "model_sku": config.model_sku,
                    },
                ),
            )
        )
    else:
        resources = resolve_existing(cli, config=config)
        project_mi_execution = config.scheduling_enabled
        requires_identity = config.project_mi_telemetry_required
        if requires_identity and not resources.project_principal_id:
            mutations.append(
                Mutation(
                    "enable_project_system_identity",
                    resources.project_resource_id,
                )
            )
        if not list_app_insights_connections(cli, resources.project_resource_id):
            connection_plan = plan_existing_connections(
                cli,
                project_resource_id=resources.project_resource_id,
                application_insights_resource_id=
                    resources.application_insights_resource_id,
            )
            mutations.append(
                Mutation(
                    "create_app_insights_connections",
                    resources.project_resource_id,
                    {
                        "application_insights_resource_id":
                            resources.application_insights_resource_id,
                        **connection_plan,
                    },
                )
            )
        if config.agent_type and (
            resources.project_principal_id or not requires_identity
        ):
            assignments = required_assignments(
                current_user_id=context.user_object_id,
                project_principal_id=resources.project_principal_id,
                foundry_account_id=resources.foundry_account_resource_id,
                project_id=resources.project_resource_id,
                application_insights_id=resources.application_insights_resource_id,
                workspace_id=resources.log_analytics_workspace_resource_id,
                agent_type=config.agent_type,
                protected_trace_content=config.protected_trace_content,
                project_mi_execution=project_mi_execution,
                project_mi_telemetry=config.project_mi_telemetry_required,
                manage_hosted_agent=config.create_sample_agent,
            )
            foundry_authorized, monitoring_authorized = (
                _existing_caller_capabilities(
                    context=context,
                    resources=resources,
                )
            )
            assignments = _filter_existing_caller_assignments(
                assignments,
                context=context,
                foundry_authorized=foundry_authorized,
                monitoring_authorized=monitoring_authorized,
            )
            mutations.extend(_role_mutations(cli, assignments))
        elif requires_identity and not resources.project_principal_id:
            mutations.extend(
                _unresolved_identity_role_mutations(
                    config=config,
                    context=context,
                    resources=resources,
                )
            )
    if creates_sample_agent:
        mutations.append(
            Mutation(
                "create_sample_agent_version",
                target_agent_name,
                {
                    "agent_type": config.agent_type,
                    "immutable": True,
                    "project_mode": config.mode,
                },
            )
        )
        mutations.append(
            Mutation(
                "generate_bounded_traffic",
                target_agent_name,
                {
                    "healthy": 6,
                    "fault": 5,
                    "max_concurrency": 1 if config.agent_type == "prompt" else 2,
                },
            )
        )
    scheduling_enabled = config.scheduling_enabled
    mutations.append(Mutation("create_or_reuse_monitor", target_agent_name))
    if scheduling_enabled:
        mutations.append(Mutation("enable_monitor", target_agent_name))
        mutations.append(
            Mutation(
                "wait_for_scheduled_agent_insights_result",
                target_agent_name,
                {"require_nonempty_insights": True},
            )
        )
    else:
        mutations.append(
            Mutation(
                (
                    "run_agent_insights"
                    if creates_sample_agent
                    else "create_or_reuse_agent_insights_result"
                ),
                target_agent_name,
                {
                    "require_nonempty_insights": True,
                    "lookback_hours": config.lookback_hours,
                },
            )
        )
    expected = {
        "first_result": "nonempty",
        "first_run_trigger": "scheduled" if scheduling_enabled else "manual",
        "monitor_enabled": scheduling_enabled,
        "traffic": (
            {"healthy": 6, "fault": 5, "total": 11}
            if creates_sample_agent
            else {"generated": 0}
        ),
    }
    if config.profile == "bug-bash":
        from .quality_review import baseline_descriptor

        if not config.agent_type:
            raise OnboardingError("missing_agent_type", "Quality review requires a sample type.")
        expected["quality_baseline"] = baseline_descriptor(config.agent_type)
    return OnboardingPlan.create(
        run_id=run_id,
        config=config,
        context=context,
        mutations=mutations,
        expected=expected,
    )


def _run_dir(run_id: str) -> Path:
    return _RUNS_ROOT / validate_run_id(run_id)


def _verify_stored_plan(payload: dict[str, Any]) -> None:
    verify_plan_payload(payload)


def _apply_existing(
    cli: AzureCli,
    *,
    config: OnboardingConfig,
    context: AzureContext,
    run_id: str,
    resource_observer: ResourceObserver | None = None,
) -> tuple[ProjectResources, tuple[str, ...]]:
    initial = resolve_existing(cli, config=config)
    if resource_observer is not None:
        resource_observer("project", asdict(initial))
    if config.project_mi_telemetry_required:
        ensure_project_identity(cli, project_resource_id=initial.project_resource_id)
    project = get_project(cli, initial.project_resource_id)
    location = str(project.get("location") or "")
    connection_ids = ensure_existing_connections(
        cli,
        project_resource_id=initial.project_resource_id,
        application_insights_resource_id=initial.application_insights_resource_id,
        location=location,
        run_id=run_id,
        **({"resource_observer": resource_observer} if resource_observer else {}),
    )
    resources = resolve_existing(cli, config=config)
    if config.project_mi_telemetry_required and not resources.project_principal_id:
        raise OnboardingError(
            "project_identity_failed",
            "Project identity was still unavailable after update.",
        )
    return resources, connection_ids


def _ensure_roles(
    cli: AzureCli,
    *,
    config: OnboardingConfig,
    context: AzureContext,
    resources: ProjectResources,
    resource_observer: ResourceObserver | None = None,
) -> tuple[dict[str, str], ...]:
    if not config.agent_type:
        raise OnboardingError(
            "missing_agent_type",
            "Agent type is required for the permission policy.",
        )
    assignments = required_assignments(
        current_user_id=context.user_object_id,
        project_principal_id=resources.project_principal_id,
        foundry_account_id=resources.foundry_account_resource_id,
        project_id=resources.project_resource_id,
        application_insights_id=resources.application_insights_resource_id,
        workspace_id=resources.log_analytics_workspace_resource_id,
        agent_type=config.agent_type,
        protected_trace_content=config.protected_trace_content,
        project_mi_execution=config.scheduling_enabled,
        project_mi_telemetry=config.project_mi_telemetry_required,
        manage_hosted_agent=config.creates_sample_agent,
    )
    if config.mode == "scratch":
        deadline = time.monotonic() + 90
        missing = missing_assignments(cli, assignments)
        while missing and time.monotonic() < deadline:
            time.sleep(10)
            missing = missing_assignments(cli, assignments)
        if missing:
            raise OnboardingError(
                "scratch_role_assignment_missing",
                "Scratch deployment did not create every planned role assignment.",
                {
                    "missing": [
                        {
                            "role_definition_id": item.role.definition_id,
                            "scope": item.scope,
                        }
                        for item in missing
                    ]
                },
            )
        return ()
    foundry_authorized, monitoring_authorized = _existing_caller_capabilities(
        context=context,
        resources=resources,
    )
    assignments = _filter_existing_caller_assignments(
        assignments,
        context=context,
        foundry_authorized=foundry_authorized,
        monitoring_authorized=monitoring_authorized,
    )
    created: list[dict[str, str]] = []
    for required in missing_assignments(cli, assignments):
        if resource_observer is not None:
            resource_observer("role_assignment_pending", {
                "id": (
                    f"{required.scope.rstrip('/')}/providers/Microsoft.Authorization/"
                    f"roleAssignments/{required.assignment_id}"
                ),
            })
        value = create_assignment(cli, required)
        identifier = str(value.get("id") or "")
        if not identifier:
            raise OnboardingError(
                "invalid_role_assignment_result",
                "Created role assignment had no resource ID.",
            )
        created.append(
            {
                "id": identifier,
                "principal_id": required.principal_id,
                "principal_type": required.principal_type,
                "role_definition_id": required.role.definition_id,
                "scope": required.scope,
                "assignment_id": required.assignment_id,
            }
        )
        if resource_observer is not None:
            resource_observer("role_assignment", created[-1])
    if created:
        time.sleep(60)
    return tuple(created)


def _report_progress(
    run_dir: Path,
    stage: str,
    callback: Callable[[dict[str, Any]], None] | None,
    **details: Any,
) -> None:
    progress = {
        "status": "in_progress",
        "stage": stage,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "plan_path": str(run_dir / "plan.json"),
        **details,
    }
    write_json_atomic(run_dir / "progress.json", progress)
    if callback is not None:
        callback(progress)


def _traffic_from_receipt(payload: dict[str, Any]) -> list[TrafficOutcome]:
    values = payload.get("outcomes")
    if not isinstance(values, list):
        raise OnboardingError(
            "invalid_traffic_receipt",
            "Traffic receipt has no outcome list.",
        )
    return [TrafficOutcome(**value) for value in values if isinstance(value, dict)]


def _wait_for_authorization(
    *,
    resources: ProjectResources,
    context: AzureContext,
    timeout_seconds: float = 300,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    credential = _credential(context)
    while True:
        with AgentInsightsClient(
            project_endpoint=resources.project_endpoint,
            credential=credential,
        ) as client:
            if client.probe().get("authorized"):
                return
        if time.monotonic() >= deadline:
            raise OnboardingError(
                "role_propagation_timeout",
                "Agent Insights remained unauthorized after role assignment.",
            )
        time.sleep(10)


def _complete_monitor(
    *,
    client: AgentInsightsClient,
    run_dir: Path,
    deployment: AgentDeployment,
    model_deployment_name: str,
    enable_monitor: bool,
    lookback_hours: int,
    allow_existing_result: bool,
    timeout_seconds: float,
    run_started_callback: Callable[[str, str, str], None] | None = None,
    required_concrete_fix_kind: str | None = None,
    quality_profile: bool = False,
) -> tuple[MonitorOutcome, bool]:
    if quality_profile and (run_dir / "quality-input.json").exists():
        from .quality_review import read_review_input

        read_review_input(run_dir)
        snapshot = read_json(run_dir / "insights-receipt.json")
        values = {
            field.name: snapshot[field.name]
            for field in fields(MonitorOutcome)
            if field.name in snapshot
        }
        values["insight_ids"] = tuple(snapshot["insight_ids"])
        return MonitorOutcome(**values), bool(snapshot["monitor_created"])

    def concrete_fix_counts(
        insights: Sequence[Mapping[str, Any]],
    ) -> tuple[int, int]:
        code_count = 0
        prompt_count = 0
        for insight in insights:
            details = insight.get("details")
            actions = (
                details.get("recommended_actions")
                if isinstance(details, Mapping)
                else None
            )
            proposed_fix = (
                actions.get("proposed_fix")
                if isinstance(actions, Mapping)
                else None
            )
            if (
                isinstance(proposed_fix, Mapping)
                and isinstance(proposed_fix.get("changes"), list)
                and proposed_fix["changes"]
            ):
                if proposed_fix.get("kind") == "code_change":
                    code_count += 1
                elif (
                    proposed_fix.get("kind") == "prompt_change"
                    and any(
                        isinstance(change, Mapping)
                        and change.get("surface") == "instructions"
                        for change in proposed_fix["changes"]
                    )
                ):
                    prompt_count += 1
        return code_count, prompt_count

    def list_insights() -> list[Mapping[str, Any]]:
        return (
            client.list_insights(monitor_id, include_details=True)
            if required_concrete_fix_kind is not None or quality_profile
            else client.list_insights(monitor_id)
        )

    def require_demo_concrete_fix(
        insights: Sequence[Mapping[str, Any]],
        insight_run_id: str,
    ) -> tuple[int, int]:
        counts = concrete_fix_counts(insights)
        required_count = (
            counts[0]
            if required_concrete_fix_kind == "code_change"
            else counts[1]
            if required_concrete_fix_kind == "prompt_change"
            else 0
        )
        if (
            required_concrete_fix_kind is not None
            and required_count == 0
            and not quality_profile
        ):
            raise OnboardingError(
                "missing_concrete_fix",
                "The sample produced insights but no required validated concrete fix. "
                "Preserve the receipts and report this as a demo regression.",
                {
                    "monitor_id": monitor_id,
                    "run_id": insight_run_id,
                    "insight_count": len(insights),
                    "required_kind": required_concrete_fix_kind,
                },
            )
        return counts

    def schedule_fields(
        monitor: Mapping[str, Any],
    ) -> tuple[float | None, str | None]:
        raw_interval = monitor.get("run_interval_hours")
        interval = (
            float(raw_interval)
            if isinstance(raw_interval, (int, float))
            else None
        )
        next_run = str(monitor.get("next_scheduled_run_at") or "") or None
        return interval, next_run

    state_path = run_dir / "insights-state.json"
    if quality_profile:
        if enable_monitor:
            raise OnboardingError(
                "bug_bash_scheduling_unsupported", "Quality review requires a manual run."
            )
        state = _admit_quality_run(
            client=client,
            run_dir=run_dir,
            deployment=deployment,
            model_deployment_name=model_deployment_name,
            lookback_hours=lookback_hours,
        )
        monitor_id = str(state["monitor_id"])
        run_id = str(state["run_id"])
        created = bool(state["monitor_created"])
        run_trigger = "manual"
        monitor_enabled_by_workflow = False
        if run_started_callback is not None:
            run_started_callback(monitor_id, run_id, run_trigger)
    elif state_path.exists():
        state = read_json(state_path)
        monitor_id = str(state.get("monitor_id") or "")
        run_id = str(state.get("run_id") or "")
        created = bool(state.get("monitor_created"))
        run_trigger = str(state.get("run_trigger") or "manual")
        monitor_enabled_by_workflow = bool(
            state.get("monitor_enabled_by_workflow")
        )
    else:
        monitor, created = client.get_or_create_monitor(
            agent_name=deployment.name,
            model_deployment_name=model_deployment_name,
        )
        monitor_id = str(monitor.get("id") or "")
        if not created and allow_existing_result:
            insights = list_insights()
            succeeded_runs = [
                run
                for run in client.list_runs(monitor_id)
                if str(run.get("status") or "").casefold() == "succeeded"
            ]
            if insights and succeeded_runs:
                reused_run_id = str(succeeded_runs[0].get("id") or "")
                code_fix_count, prompt_fix_count = require_demo_concrete_fix(
                    insights,
                    reused_run_id,
                )
                final_monitor = monitor
                if enable_monitor and not bool(monitor.get("enabled")):
                    final_monitor = client.enable_monitor(monitor_id)
                interval, next_run = schedule_fields(final_monitor)
                outcome = MonitorOutcome(
                    monitor_id=monitor_id,
                    run_id=reused_run_id,
                    insight_ids=tuple(
                        str(item.get("id") or "") for item in insights
                    ),
                    estimated_cost=(
                        dict(final_monitor["estimated_cost"])
                        if isinstance(final_monitor.get("estimated_cost"), dict)
                        else None
                    ),
                    enabled=bool(final_monitor.get("enabled")),
                    concrete_code_fix_count=code_fix_count,
                    concrete_prompt_fix_count=prompt_fix_count,
                    run_trigger=str(
                        succeeded_runs[0].get("trigger") or "existing"
                    ).casefold(),
                    run_interval_hours=interval,
                    next_scheduled_run_at=next_run,
                )
                write_json_atomic(
                    run_dir / "insights-receipt.json",
                    {
                        "status": "complete",
                        **asdict(outcome),
                        "monitor_created": False,
                        "reused_existing_run": True,
                    },
                )
                return outcome, False
        monitor_enabled_by_workflow = False
        if enable_monitor:
            existing_runs = client.list_runs(monitor_id)
            excluded_run_ids = {
                str(run.get("id") or "")
                for run in existing_runs
                if run.get("id")
            }
            if not bool(monitor.get("enabled")):
                monitor = client.enable_monitor(monitor_id)
                monitor_enabled_by_workflow = True
            else:
                active_scheduled_runs = [
                    run
                    for run in existing_runs
                    if str(run.get("trigger") or "").casefold() == "scheduled"
                    and str(run.get("status") or "").casefold()
                    not in {"succeeded", "failed", "canceled", "cancelled"}
                ]
                if active_scheduled_runs:
                    excluded_run_ids.discard(
                        str(active_scheduled_runs[0].get("id") or "")
                    )
            try:
                run = client.wait_new_scheduled_run(
                    monitor_id=monitor_id,
                    excluded_run_ids=excluded_run_ids,
                )
            except OnboardingError as error:
                if monitor_enabled_by_workflow:
                    try:
                        client.disable_monitor(monitor_id)
                    except OnboardingError as rollback_error:
                        raise OnboardingError(
                            "scheduled_run_admission_failed_and_rollback_failed",
                            "The immediate scheduled run was not admitted, and the "
                            "workflow could not disable the newly enabled monitor.",
                            {
                                "monitor_id": monitor_id,
                                "run_error": error.code,
                                "rollback_error": rollback_error.code,
                            },
                        ) from error
                raise
            run_trigger = "scheduled"
        else:
            run = client.create_run(monitor_id, lookback_hours=lookback_hours)
            run_trigger = "manual"
        run_id = str(run.get("id") or "")
        write_json_atomic(
            state_path,
            {
                "status": "started",
                "monitor_id": monitor_id,
                "run_id": run_id,
                "monitor_created": created,
                "monitor_enabled_by_workflow": monitor_enabled_by_workflow,
                "run_trigger": run_trigger,
            },
        )
        if run_started_callback is not None:
            run_started_callback(monitor_id, run_id, run_trigger)
    try:
        client.wait_run(
            monitor_id=monitor_id,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
        )
        insights = list_insights()
        if quality_profile:
            runs = client.list_runs(monitor_id)
            if (
                len(runs) != 1
                or str(runs[0].get("id") or "") != run_id
                or normalize_run_trigger(runs[0].get("trigger")) != "manual"
            ):
                raise OnboardingError(
                    "quality_run_scope_ambiguous",
                    "The monitor no longer has a single confirmed manual run. "
                    "Its monitor-wide insights cannot be attributed to this run safely.",
                    {"monitor_id": monitor_id, "run_id": run_id},
                )
            state["verified_run_ids"] = [run_id]
        if not insights and not quality_profile:
            raise OnboardingError(
                "empty_insights",
                "Agent Insights run succeeded but returned no insights.",
                {"monitor_id": monitor_id, "run_id": run_id},
            )
        code_fix_count, prompt_fix_count = require_demo_concrete_fix(
            insights,
            run_id,
        )
    except OnboardingError as error:
        if monitor_enabled_by_workflow:
            try:
                client.disable_monitor(monitor_id)
            except OnboardingError as rollback_error:
                raise OnboardingError(
                    "scheduled_first_run_failed_and_rollback_failed",
                    "The immediate scheduled run failed, and the workflow could not "
                    "disable the newly enabled monitor.",
                    {
                        "monitor_id": monitor_id,
                        "run_id": run_id,
                        "run_error": error.code,
                        "rollback_error": rollback_error.code,
                    },
                ) from error
        raise
    monitor = client.get_monitor(monitor_id)
    if quality_profile and bool(monitor.get("enabled")):
        raise OnboardingError(
            "bug_bash_monitor_changed",
            "The owned one-off monitor was enabled outside this workflow; review its state.",
            {"monitor_id": monitor_id, "run_id": run_id},
        )
    interval, next_run = schedule_fields(monitor)
    outcome = MonitorOutcome(
        monitor_id=monitor_id,
        run_id=run_id,
        insight_ids=tuple(str(item.get("id") or "") for item in insights),
        estimated_cost=(
            dict(monitor["estimated_cost"])
            if isinstance(monitor.get("estimated_cost"), dict)
            else None
        ),
        enabled=bool(monitor.get("enabled")),
        concrete_code_fix_count=code_fix_count,
        concrete_prompt_fix_count=prompt_fix_count,
        run_trigger=run_trigger,
        run_interval_hours=interval,
        next_scheduled_run_at=next_run,
    )
    write_json_atomic(
        run_dir / "insights-receipt.json",
        {"status": "complete", **asdict(outcome), "monitor_created": created},
    )
    if quality_profile:
        from .quality_review import prepare_review

        state["insight_collection_complete"] = getattr(
            client, "insights_collection_complete", None
        )
        write_json_atomic(state_path, state)
        prepare_review(run_dir, monitor=outcome, insights=insights)
    return outcome, created


def _admit_quality_run(
    *,
    client: AgentInsightsClient,
    run_dir: Path,
    deployment: AgentDeployment,
    model_deployment_name: str,
    lookback_hours: int,
) -> dict[str, Any]:
    path = run_dir / "insights-state.json"
    if path.exists():
        state = read_json(path)
        if (
            state.get("agent_name") != deployment.name
            or state.get("agent_version") != deployment.version
            or state.get("run_trigger") != "manual"
        ):
            raise OnboardingError(
                "insights_state_mismatch", "Saved Insights state belongs to a different sample."
            )
    else:
        state = {
            "status": "creating_monitor",
            "agent_name": deployment.name,
            "agent_version": deployment.version,
            "run_trigger": "manual",
            "monitor_created": False,
            "monitor_enabled_by_workflow": False,
        }
        write_json_atomic(path, state)
        monitor, created = client.get_or_create_monitor(
            agent_name=deployment.name, model_deployment_name=model_deployment_name
        )
        if not created or bool(monitor.get("enabled")):
            raise OnboardingError(
                "quality_monitor_not_owned",
                "Quality review requires a new disabled monitor owned by this sample run.",
            )
        state.update(
            status="monitor_ready",
            monitor_id=str(monitor.get("id") or ""),
            monitor_created=True,
        )
        write_json_atomic(path, state)
    monitor_id = str(state.get("monitor_id") or "")
    if not monitor_id or not state.get("monitor_created"):
        raise OnboardingError(
            "monitor_creation_unconfirmed",
            "Monitor creation has an uncertain outcome. Preserve the run for reconciliation.",
            {"run_dir": str(run_dir)},
        )
    live_monitor = client.get_monitor(monitor_id)
    if (
        str(live_monitor.get("id") or "") != monitor_id
        or str(live_monitor.get("agent_name") or "") != deployment.name
        or bool(live_monitor.get("enabled"))
    ):
        raise OnboardingError(
            "quality_monitor_mismatch",
            "The live monitor no longer matches this sample's disabled one-off monitor.",
            {"monitor_id": monitor_id},
        )
    if state.get("run_id"):
        return state
    if state.get("status") == "admitting":
        known = set(state.get("known_run_ids") or [])
        candidates = [
            run for run in client.list_runs(monitor_id)
            if run.get("id") not in known
            and normalize_run_trigger(run.get("trigger")) == "manual"
        ]
        if len(candidates) != 1:
            raise OnboardingError(
                "insights_admission_unconfirmed",
                "The manual request has an uncertain outcome; status will only reconcile it, "
                "never submit replacement traffic or a second Insights request.",
                {"monitor_id": monitor_id, "candidate_count": len(candidates)},
            )
        run = candidates[0]
    elif state.get("status") == "monitor_ready":
        known_runs = client.list_runs(monitor_id)
        if known_runs:
            raise OnboardingError(
                "quality_monitor_already_used",
                "The newly owned quality monitor already has runs; no new run was submitted.",
            )
        state.update(status="admitting", known_run_ids=[])
        write_json_atomic(path, state)
        run = client.create_run(monitor_id, lookback_hours=lookback_hours)
    else:
        raise OnboardingError("invalid_insights_state", "Unrecognized manual admission state.")
    run_id = str(run.get("id") or "")
    if not run_id:
        raise OnboardingError("invalid_insights_run", "Manual run response has no identifier.")
    state.update(status="started", run_id=run_id)
    write_json_atomic(path, state)
    return state


def _review_workflow_status(review: Mapping[str, Any]) -> str:
    return (
        "complete"
        if review["ai_status"] == "recorded" and review["human_status"] == "rated"
        else "review_pending"
    )


def _finalize(
    *,
    run_dir: Path,
    plan: dict[str, Any],
    resources: ProjectResources,
    deployment: AgentDeployment,
    monitor: MonitorOutcome,
    context: AzureContext,
) -> dict[str, Any]:
    cleanup_command = [
        sys.executable,
        str(_SKILL_ROOT / "scripts" / "agent_insights_onboard.py"),
        "cleanup",
        "--run-dir",
        str(run_dir),
    ]
    insight_count = len(monitor.insight_ids)
    project_portal_url = foundry_project_url(
        resources.project_resource_id,
        context.tenant_id,
    )
    insights_portal_url = agent_insights_url(
        resources.project_resource_id,
        context.tenant_id,
        deployment.name,
    )
    monitor_payload = asdict(monitor)
    monitor_payload["insight_count"] = insight_count
    final = {
        "status": "complete",
        "run_id": plan["run_id"],
        "plan_hash": plan["plan_hash"],
        "mode": plan["mode"],
        "project": asdict(resources),
        "agent": asdict(deployment),
        "monitor": monitor_payload,
        "result_summary": {
            "insight_count": insight_count,
            "concrete_code_fix_count": monitor.concrete_code_fix_count,
            "concrete_prompt_fix_count": monitor.concrete_prompt_fix_count,
            "first_run_trigger": monitor.run_trigger,
            "message": (
                f"Agent Insights returned {insight_count} insight"
                + ("" if insight_count == 1 else "s")
                + " for the first verified result."
            ),
            "schedule_enabled": monitor.enabled,
            "run_interval_hours": monitor.run_interval_hours,
            "next_scheduled_run_at": monitor.next_scheduled_run_at,
        },
        "foundry_portal_url": insights_portal_url,
        "foundry_project_url": project_portal_url,
        "agent_insights_portal_url": insights_portal_url,
        "feedback_url": _FEEDBACK_URL,
        "portal_instructions": (
            "Open the Agent Insights link. If the portal redirects to project home, "
            "select Monitor, choose the agent, and open Agent Insights."
        ),
        "azure_resource_url": (
            f"https://portal.azure.com/#@{context.tenant_id}/resource"
            f"{resources.project_resource_id}"
        ),
        "cleanup_command": cleanup_command,
        "receipt_path": str(run_dir / "final-receipt.json"),
    }
    if plan["config"].get("profile") == "bug-bash":
        from .quality_review import review_status

        review = review_status(run_dir)
        final["status"] = _review_workflow_status(review)
        final["quality_review"] = review
        final["result_summary"]["message"] = (
            f"Returned {insight_count} insights; AI assessment and overall human feedback "
            "are separate from successful execution."
        )
    write_json_atomic(run_dir / "final-receipt.json", final)
    return final


def _refresh_project_access(
    run_dir: Path,
    plan: dict[str, Any],
    config: OnboardingConfig,
    context: AzureContext,
    resources: ProjectResources,
    cli: AzureCli,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    group_name = resource_group_name(config, str(plan["run_id"]))
    group_id = f"/subscriptions/{config.subscription_id}/resourceGroups/{group_name}"
    group = cli.json(["group", "show", "--name", group_name])
    if not isinstance(group, Mapping) or str(group.get("id", "")).casefold() != group_id.casefold():
        raise OnboardingError("ownership_mismatch", "Prepared resource group identity changed.")
    require_owned_tags(
        group.get("tags"), run_id=str(plan["run_id"]), owner_object_id=context.user_object_id
    )
    for identifier in (
        resources.project_resource_id, resources.foundry_account_resource_id,
        resources.application_insights_resource_id, resources.log_analytics_workspace_resource_id,
    ):
        parsed = parse_resource_id(identifier)
        if (
            parsed.subscription_id.casefold() != config.subscription_id.casefold()
            or parsed.resource_group.casefold() != group_name.casefold()
        ):
            raise OnboardingError("ownership_mismatch", "Access target is outside the owned group.")
    assignments = required_assignments(
        current_user_id=context.user_object_id,
        project_principal_id=resources.project_principal_id,
        foundry_account_id=resources.foundry_account_resource_id,
        project_id=resources.project_resource_id,
        application_insights_id=resources.application_insights_resource_id,
        workspace_id=resources.log_analytics_workspace_resource_id,
        agent_type=config.agent_type or "",
        protected_trace_content=config.protected_trace_content,
        project_mi_execution=config.scheduling_enabled,
        project_mi_telemetry=config.project_mi_telemetry_required,
        manage_hosted_agent=True,
    )
    missing = missing_assignments(cli, assignments)
    _require_assignment_write(cli, {item.scope for item in missing}, missing)
    refresh_dir = run_dir / "access-refreshes" / secrets.token_hex(6)
    access_plan = OnboardingPlan.create(
        run_id=str(plan["run_id"]), config=config, context=context,
        mutations=[Mutation("create_role_assignment", item.scope, {
            "principal_id": item.principal_id, "principal_type": item.principal_type,
            "role_definition_id": item.role.definition_id, "role_name": item.role.name,
            "assignment_id": item.assignment_id,
        }) for item in missing],
        expected={"operation": "refresh_project_access", "parent_run_dir": str(run_dir)},
    ).as_dict()
    write_json_atomic(refresh_dir / "plan.json", access_plan)
    journal = ProvisioningJournal(refresh_dir, access_plan)
    _report_progress(
        run_dir, "project_access_refresh", progress_callback,
        access_plan_path=str(refresh_dir / "plan.json"), access_plan=access_plan,
    )
    created = []
    for item in missing:
        identifier = (
            f"{item.scope}/providers/Microsoft.Authorization/roleAssignments/{item.assignment_id}"
        )
        journal.observe("role_assignment_pending", {"id": identifier})
        value = create_assignment(cli, item)
        if str(value.get("id", "")).casefold() != identifier.casefold():
            raise OnboardingError(
                "role_assignment_mismatch", "Access assignment ID was unexpected."
            )
        record = {
            "id": identifier, "principal_id": item.principal_id,
            "principal_type": item.principal_type, "role_definition_id": item.role.definition_id,
            "scope": item.scope, "assignment_id": item.assignment_id,
        }
        journal.observe("role_assignment", record)
        created.append(record)
    if created:
        time.sleep(60)
    if missing_assignments(cli, assignments):
        raise OnboardingError("role_propagation_timeout", "Prepared access is not visible yet.")
    result = {
        "status": "access_updated", "run_id": plan["run_id"],
        "project": asdict(resources), "created_role_assignments": created,
        "receipt_path": str(refresh_dir / "access-receipt.json"),
        "parent_receipt_path": str(run_dir / "final-receipt.json"),
    }
    write_json_atomic(refresh_dir / "access-receipt.json", result)
    return result


def prepare_project(
    config: OnboardingConfig,
    *,
    run_id: str | None = None,
    dry_run: bool = False,
    refresh_access: bool = False,
    cli: AzureCli | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Prepare owned organizer infrastructure without creating an Agent or traffic."""
    if config.mode != "scratch" or config.profile != "bug-bash":
        raise OnboardingError(
            "invalid_project_preparation",
            "Project preparation requires --mode scratch --profile bug-bash.",
        )
    _validate_agent_selection(config)
    selected_cli = cli or AzureCli()
    selected_run_id = validate_run_id(run_id) if run_id else secrets.token_hex(6)
    run_dir = _run_dir(selected_run_id)
    if (run_dir / "cleanup-receipt.json").exists():
        raise OnboardingError("run_already_cleaned", "This preparation run was already cleaned.")
    context = select_context(selected_cli, config.subscription_id)
    plan_path = run_dir / "plan.json"
    plan: dict[str, Any] | None = None
    if plan_path.exists():
        plan = read_json(plan_path)
        _verify_stored_plan(plan)
        if (
            OnboardingConfig(**plan["config"]) != config
            or plan["expected"].get("operation") != "prepare_project"
        ):
            raise OnboardingError(
                "plan_configuration_mismatch",
                "This run belongs to a different operation or config.",
            )
        validate_plan_context(
            OnboardingPlan(**plan),
            subscription_id=context.subscription_id, tenant_id=context.tenant_id,
            user_object_id=context.user_object_id,
        )
        final_path = run_dir / "final-receipt.json"
        if final_path.exists() and not dry_run:
            final = read_json(final_path)
            if refresh_access:
                return _refresh_project_access(
                    run_dir, plan, config, context, ProjectResources(**final["project"]),
                    selected_cli, progress_callback,
                )
            return final
    if refresh_access:
        raise OnboardingError(
            "project_preparation_required",
            "Access refresh requires a completed owned project-preparation receipt.",
        )
    doctor(config, selected_cli)
    if plan is None:
        full_plan = build_plan(config, context=context, run_id=selected_run_id, cli=selected_cli)
        plan = OnboardingPlan.create(
            run_id=selected_run_id, config=config, context=context,
            mutations=[
                Mutation(**mutation) for mutation in full_plan.mutations
                if mutation["kind"] in {"create_resource_group", "deploy_scratch_environment"}
            ],
            expected={
                "operation": "prepare_project", "traffic": {"generated": 0},
                "monitor_enabled": False,
            },
        ).as_dict()
        write_json_atomic(plan_path, plan)
    if dry_run:
        return {"status": "planned_no_writes", "plan_path": str(plan_path), "plan": plan}
    context = select_context(selected_cli, config.subscription_id)
    validate_plan_context(
        OnboardingPlan(**plan),
        subscription_id=context.subscription_id, tenant_id=context.tenant_id,
        user_object_id=context.user_object_id,
    )
    journal = ProvisioningJournal(run_dir, plan)
    _report_progress(run_dir, "project_provisioning", progress_callback, plan=plan)
    resources = provision_scratch(
        selected_cli, config=config, context=context, run_id=selected_run_id,
        resource_observer=journal.observe,
    )
    journal.observe("project", asdict(resources))
    _report_progress(run_dir, "project_permissions", progress_callback)
    _ensure_roles(
        selected_cli, config=config, context=context, resources=resources,
        resource_observer=journal.observe,
    )
    _wait_for_authorization(resources=resources, context=context)
    receipt = {
        "status": "complete", "operation": "prepare_project",
        "run_id": selected_run_id, "plan_hash": plan["plan_hash"], "mode": config.mode,
        "project": asdict(resources), "agent_created": False,
        "created_connection_ids": journal.payload["created_connection_ids"],
        "created_role_assignments": journal.payload["created_role_assignments"],
    }
    write_json_atomic(run_dir / "provisioning-receipt.json", receipt)
    final = {
        **receipt, "status": "project_ready", "traffic_generated": 0,
        "foundry_project_url": foundry_project_url(
            resources.project_resource_id, context.tenant_id
        ),
        "receipt_path": str(run_dir / "final-receipt.json"),
        "cleanup_command": [
            sys.executable, str(_SKILL_ROOT / "scripts" / "agent_insights_onboard.py"),
            "cleanup", "--run-dir", str(run_dir),
        ],
    }
    write_json_atomic(run_dir / "final-receipt.json", final)
    return final


def onboard(
    config: OnboardingConfig,
    *,
    run_id: str | None = None,
    dry_run: bool = False,
    ingestion_timeout_seconds: float = 900,
    insights_timeout_seconds: float = 21600,
    cli: AzureCli | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    resource_observer: ResourceObserver | None = None,
) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    doctor(config, selected_cli)
    context = select_context(selected_cli, config.subscription_id)
    selected_run_id = validate_run_id(run_id) if run_id else secrets.token_hex(6)
    run_dir = _run_dir(selected_run_id)
    if (run_dir / "cleanup-receipt.json").exists():
        raise OnboardingError(
            "run_already_cleaned", "This run has been cleaned up; choose a new run ID."
        )
    plan_path = run_dir / "plan.json"
    if plan_path.exists():
        plan_payload = read_json(plan_path)
        _verify_stored_plan(plan_payload)
        if plan_payload["expected"].get("operation") == "prepare_project":
            raise OnboardingError(
                "infrastructure_only_run",
                "This run prepares shared infrastructure only. Create a separate existing-project "
                "sample run instead of invoking an Agent under the infrastructure receipt.",
            )
        if OnboardingConfig(**plan_payload["config"]) != config:
            raise OnboardingError(
                "plan_configuration_mismatch",
                "Existing run ID was planned with different configuration.",
            )
    else:
        plan = build_plan(
            config,
            context=context,
            run_id=selected_run_id,
            cli=selected_cli,
        )
        plan_payload = plan.as_dict()
        write_json_atomic(plan_path, plan_payload)
    if config.profile == "bug-bash":
        from .quality_review import baseline_descriptor

        if (
            not config.agent_type
            or plan_payload["expected"].get("quality_baseline")
            != baseline_descriptor(config.agent_type)
        ):
            raise OnboardingError(
                "quality_baseline_changed",
                "Sample assets changed after planning; preserve this run "
                "and use its original assets.",
            )
    if dry_run:
        return {
            "status": "planned_no_writes",
            "plan_path": str(plan_path),
            "plan": plan_payload,
        }
    if (run_dir / "traffic-receipt.json").exists():
        raise OnboardingError(
            "traffic_already_generated",
            "This run already generated traffic; use status instead of onboard.",
            {"run_dir": str(run_dir)},
        )
    live_context = select_context(selected_cli, config.subscription_id)
    plan_object = OnboardingPlan(**plan_payload)
    validate_plan_context(
        plan_object,
        subscription_id=live_context.subscription_id,
        tenant_id=live_context.tenant_id,
        user_object_id=live_context.user_object_id,
    )
    journal = ProvisioningJournal(run_dir, plan_payload)
    if journal.payload["pending_connection_ids"] or journal.payload["pending_role_assignment_ids"]:
        raise OnboardingError(
            "provisioning_outcome_unconfirmed",
            "An earlier mutation has an uncertain outcome; reconcile ownership or run cleanup "
            "before starting a new run. This run will not replay the write.",
            {"run_dir": str(run_dir)},
        )

    def observe_resource(kind: str, value: Mapping[str, Any]) -> None:
        journal.observe(kind, value)
        if resource_observer is not None:
            resource_observer(kind, value)

    _report_progress(
        run_dir, "provisioning", progress_callback, plan=plan_payload
    )
    creates_sample_agent = config.creates_sample_agent
    if config.mode == "scratch":
        resources = provision_scratch(
            selected_cli,
            config=config,
            context=live_context,
            run_id=selected_run_id,
            resource_observer=observe_resource,
        )
        connection_ids: tuple[str, ...] = ()
    else:
        resources, connection_ids = _apply_existing(
            selected_cli,
            config=config,
            context=live_context,
            run_id=selected_run_id,
            resource_observer=observe_resource,
        )
    observe_resource("project", asdict(resources))
    _report_progress(run_dir, "permissions", progress_callback)
    created_roles = _ensure_roles(
        selected_cli,
        config=config,
        context=live_context,
        resources=resources,
        resource_observer=observe_resource,
    )
    for assignment in created_roles:
        observe_resource("role_assignment", assignment)
    for connection_id in connection_ids:
        observe_resource("connection", {"id": connection_id})
    _wait_for_authorization(resources=resources, context=live_context)
    _report_progress(run_dir, "agent_deployment", progress_callback)
    project = project_client(resources.project_endpoint, live_context.tenant_id)
    if creates_sample_agent:
        if not config.agent_type:
            raise OnboardingError("missing_agent_type", "Sample Agent type is missing.")
        deployment = create_sample_agent(
            project,
            run_id=selected_run_id,
            agent_type=config.agent_type,
            model=resources.model_deployment_name,
            resource_observer=observe_resource,
            capture_sample_digest=config.profile == "bug-bash",
            allow_owned_reuse=config.profile == "standard"
            or bool(journal.payload.get("agent_creation_started") or journal.payload.get("agent")),
        )
        observe_resource("agent", asdict(deployment))
        deployment = AgentDeployment(**journal.payload["agent"])
    else:
        deployment = validate_existing_agent(project, name=config.agent_name or "")
        if deployment.kind != config.agent_type:
            raise OnboardingError(
                "agent_type_mismatch",
                "Existing Agent kind differs from the selected permission policy.",
                {"selected": config.agent_type, "actual": deployment.kind},
            )
    provisioning_receipt = {
        "status": "complete",
        "run_id": selected_run_id,
        "plan_hash": plan_payload["plan_hash"],
        "mode": config.mode,
        "project": asdict(resources),
        "agent": asdict(deployment),
        "agent_created": creates_sample_agent,
        "created_role_assignments": journal.payload["created_role_assignments"],
        "created_role_assignment_ids": [
            item["id"] for item in journal.payload["created_role_assignments"]
        ],
        "created_connection_ids": journal.payload["created_connection_ids"],
        "pending_connection_ids": journal.payload["pending_connection_ids"],
        "pending_role_assignment_ids": journal.payload["pending_role_assignment_ids"],
        "agent_creation_started": journal.payload.get("agent_creation_started", False),
    }
    write_json_atomic(run_dir / "provisioning-receipt.json", provisioning_receipt)
    _report_progress(run_dir, "traffic", progress_callback)
    traffic_payload: dict[str, Any] = {
        "status": (
            "generating"
            if creates_sample_agent
            else "using_existing"
        ),
        "run_id": selected_run_id,
        "agent": asdict(deployment),
        "outcomes": [],
    }
    write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)

    observed_outcomes: list[TrafficOutcome] = []

    def record_outcome(outcome: TrafficOutcome) -> None:
        observed_outcomes.append(outcome)
        observed_outcomes.sort(key=lambda item: item.scenario)
        traffic_payload["outcomes"] = [
            asdict(item) for item in observed_outcomes
        ]
        write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)

    try:
        if creates_sample_agent:
            outcomes = generate_sample_traffic(
                project,
                deployment,
                outcome_observer=record_outcome,
                collect_sample_evidence=config.profile == "bug-bash",
            )
        else:
            outcomes = []
    except BaseException:
        traffic_payload["status"] = "failed_partial"
        traffic_payload["outcomes"] = [
            asdict(item) for item in observed_outcomes
        ]
        write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)
        raise
    traffic_payload["status"] = "generated" if outcomes else "using_existing"
    traffic_payload["outcomes"] = [asdict(item) for item in outcomes]
    write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)
    credential = _credential(live_context)
    _report_progress(run_dir, "ingestion", progress_callback)
    if outcomes:
        ingestion_evidence = wait_for_ingestion(
            credential=credential,
            application_insights_resource_id=
                resources.application_insights_resource_id,
            deployment=deployment,
            outcomes=outcomes,
            timeout_seconds=ingestion_timeout_seconds,
            require_tool=deployment.kind == "hosted",
        )
    else:
        ingestion_evidence = require_recent_agent_roots(
            credential=credential,
            application_insights_resource_id=
                resources.application_insights_resource_id,
            deployment=deployment,
            lookback_hours=config.lookback_hours,
        )
    traffic_payload["status"] = "ingested"
    traffic_payload["ingestion_evidence"] = ingestion_evidence
    write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)
    _report_progress(run_dir, "insights_admission", progress_callback)

    def report_run_started(
        monitor_id: str,
        insights_run_id: str,
        run_trigger: str,
    ) -> None:
        receipt_path = run_dir / "run-started-receipt.json"
        progress = {
            "status": "insights_running",
            "onboarding_run_id": selected_run_id,
            "monitor_id": monitor_id,
            "insights_run_id": insights_run_id,
            "insights_run_trigger": run_trigger,
            "agent_insights_portal_url": agent_insights_url(
                resources.project_resource_id,
                live_context.tenant_id,
                deployment.name,
            ),
            "first_run_estimated_minutes": {
                "minimum": 10,
                "maximum": 20,
            },
            "message": (
                "Open the Agent Insights portal now. The first run may take "
                "10-20 minutes; onboarding will continue monitoring it."
            ),
            "receipt_path": str(receipt_path),
        }
        write_json_atomic(receipt_path, progress)
        if progress_callback is not None:
            progress_callback(progress)

    with AgentInsightsClient(
        project_endpoint=resources.project_endpoint,
        credential=credential,
    ) as insights:
        feature = insights.probe()
        if not feature.get("authorized"):
            raise OnboardingError(
                "agent_insights_forbidden",
                "Agent Insights remained unauthorized after role propagation.",
            )
        monitor, _ = _complete_monitor(
            client=insights,
            run_dir=run_dir,
            deployment=deployment,
            model_deployment_name=resources.model_deployment_name,
            enable_monitor=config.scheduling_enabled,
            lookback_hours=config.lookback_hours,
            allow_existing_result=config.mode == "existing" and config.profile == "standard",
            timeout_seconds=insights_timeout_seconds,
            run_started_callback=report_run_started,
            required_concrete_fix_kind=(
                "prompt_change"
                if creates_sample_agent and deployment.kind == "prompt"
                else "code_change"
                if creates_sample_agent and deployment.kind == "hosted"
                else None
            ),
            quality_profile=config.profile == "bug-bash",
        )
    return _finalize(
        run_dir=run_dir,
        plan=plan_payload,
        resources=resources,
        deployment=deployment,
        monitor=monitor,
        context=live_context,
    )


def status(
    run_dir: Path,
    *,
    ingestion_timeout_seconds: float = 900,
    insights_timeout_seconds: float = 21600,
    cli: AzureCli | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    final_path = run_dir / "final-receipt.json"
    if final_path.exists():
        final = read_json(final_path)
        if "quality_review" in final:
            from .quality_review import review_status

            review = review_status(run_dir)
            final["quality_review"] = review
            final["status"] = _review_workflow_status(review)
            write_json_atomic(final_path, final)
        return final
    plan = read_json(run_dir / "plan.json")
    _verify_stored_plan(plan)
    provisioning = read_json(run_dir / "provisioning-receipt.json")
    traffic = read_json(run_dir / "traffic-receipt.json")
    config = OnboardingConfig(**plan["config"])
    _validate_agent_selection(config)
    context = select_context(selected_cli, config.subscription_id)
    validate_plan_context(
        OnboardingPlan(**plan),
        subscription_id=context.subscription_id,
        tenant_id=context.tenant_id,
        user_object_id=context.user_object_id,
    )
    resources = ProjectResources(**provisioning["project"])
    deployment = AgentDeployment(**provisioning["agent"])
    _report_progress(run_dir, "resuming", progress_callback)
    if traffic.get("status") in {"generating", "failed_partial"}:
        raise OnboardingError(
            "partial_traffic_not_resumable",
            "This run generated only partial traffic and cannot be replayed. "
            "Preserve the receipt for diagnosis, clean up the run, and start a new run.",
            {
                "observed_outcome_count": len(traffic.get("outcomes") or []),
                "traffic_status": traffic.get("status"),
            },
        )
    outcomes = _traffic_from_receipt(traffic)
    credential = _credential(context)
    if "ingestion_evidence" not in traffic:
        if outcomes:
            evidence = wait_for_ingestion(
                credential=credential,
                application_insights_resource_id=
                    resources.application_insights_resource_id,
                deployment=deployment,
                outcomes=outcomes,
                timeout_seconds=ingestion_timeout_seconds,
                require_tool=deployment.kind == "hosted",
            )
        else:
            evidence = require_recent_agent_roots(
                credential=credential,
                application_insights_resource_id=
                    resources.application_insights_resource_id,
                deployment=deployment,
                lookback_hours=config.lookback_hours,
            )
        traffic["status"] = "ingested"
        traffic["ingestion_evidence"] = evidence
        write_json_atomic(run_dir / "traffic-receipt.json", traffic)
    with AgentInsightsClient(
        project_endpoint=resources.project_endpoint,
        credential=credential,
    ) as insights:
        monitor, _ = _complete_monitor(
            client=insights,
            run_dir=run_dir,
            deployment=deployment,
            model_deployment_name=resources.model_deployment_name,
            enable_monitor=config.scheduling_enabled,
            lookback_hours=config.lookback_hours,
            allow_existing_result=config.mode == "existing" and config.profile == "standard",
            timeout_seconds=insights_timeout_seconds,
            required_concrete_fix_kind=(
                "prompt_change"
                if config.creates_sample_agent
                and deployment.kind == "prompt"
                else "code_change"
                if config.creates_sample_agent
                and deployment.kind == "hosted"
                else None
            ),
            quality_profile=config.profile == "bug-bash",
        )
    return _finalize(
        run_dir=run_dir,
        plan=plan,
        resources=resources,
        deployment=deployment,
        monitor=monitor,
        context=context,
    )


def _cleanup_existing_sample_agent(
    *,
    run_dir: Path,
    plan: dict[str, Any],
    provisioning: dict[str, Any],
    resources: ProjectResources,
    context: AzureContext,
) -> None:
    agent_payload = provisioning.get("agent")
    if not isinstance(agent_payload, dict) or not provisioning.get("agent_created"):
        raise OnboardingError(
            "invalid_cleanup_receipt",
            "Existing-project sample Agent cleanup requires an ownership receipt.",
        )
    deployment = AgentDeployment(**agent_payload)
    run_id = str(plan["run_id"])
    expected_name = sample_agent_name(run_id, deployment.kind)
    planned_agents = [
        item
        for item in plan.get("mutations", [])
        if isinstance(item, dict)
        and item.get("kind") == "create_sample_agent_version"
    ]
    if (
        len(planned_agents) != 1
        or planned_agents[0].get("target") != deployment.name
        or deployment.name != expected_name
    ):
        raise OnboardingError(
            "agent_cleanup_target_mismatch",
            "Agent cleanup target is not present in the frozen plan.",
        )

    project = project_client(resources.project_endpoint, context.tenant_id)
    verify_owned_agent(project, deployment=deployment, run_id=run_id)
    monitor_state: dict[str, Any] = {}
    for name in ("insights-receipt.json", "insights-state.json"):
        path = run_dir / name
        if path.exists():
            monitor_state = read_json(path)
            break
    monitor_id = str(monitor_state.get("monitor_id") or "")
    if monitor_id:
        if not monitor_state.get("monitor_created"):
            raise OnboardingError(
                "monitor_cleanup_target_mismatch",
                "The Agent monitor was not created by this quickstart run.",
            )
        with AgentInsightsClient(
            project_endpoint=resources.project_endpoint,
            credential=_credential(context),
        ) as insights:
            try:
                live_monitor = insights.get_monitor(monitor_id)
            except OnboardingError as error:
                if not is_resource_not_found(error):
                    raise
            else:
                if (
                    str(live_monitor.get("id") or "") != monitor_id
                    or str(live_monitor.get("agent_name") or "") != deployment.name
                ):
                    raise OnboardingError(
                        "monitor_cleanup_target_mismatch",
                        "Live monitor no longer matches the quickstart receipt.",
                    )
                insights.delete_monitor(monitor_id)
    elif monitor_state:
        with AgentInsightsClient(
            project_endpoint=resources.project_endpoint,
            credential=_credential(context),
        ) as insights:
            if insights.list_monitors(deployment.name):
                raise OnboardingError(
                    "monitor_cleanup_unconfirmed",
                    "Monitor creation was not confirmed; preserve the Agent for reconciliation.",
                )
    delete_owned_agent(project, deployment=deployment, run_id=run_id)


def _finish_cleanup(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    receipt = {
        "status": "complete",
        "run_id": plan["run_id"],
        "plan_hash": plan["plan_hash"],
        "mode": plan["mode"],
        "cleaned_at": time.time(),
    }
    write_json_atomic(run_dir / "cleanup-receipt.json", receipt)
    return receipt


def _cleanup_resource(cli: AzureCli, resource_id: str, api_version: str) -> Any:
    try:
        return cli.rest(
            method="get",
            url=f"https://management.azure.com{resource_id}?api-version={api_version}",
        )
    except OnboardingError as error:
        if is_resource_not_found(error):
            return None
        raise


def cleanup(run_dir: Path, cli: AzureCli | None = None) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    plan = read_json(run_dir / "plan.json")
    _verify_stored_plan(plan)
    if plan["expected"].get("operation") == "refresh_project_access":
        raise OnboardingError(
            "parent_cleanup_required",
            "Use the parent project's cleanup command, not an access-refresh directory.",
        )
    config = OnboardingConfig(**plan["config"])
    context = select_context(selected_cli, config.subscription_id)
    validate_plan_context(
        OnboardingPlan(**plan),
        subscription_id=context.subscription_id,
        tenant_id=context.tenant_id,
        user_object_id=context.user_object_id,
    )
    cleanup_path = run_dir / "cleanup-receipt.json"
    if cleanup_path.exists():
        previous = read_json(cleanup_path)
        if previous.get("run_id") != plan["run_id"] or (
            previous.get("plan_hash") and previous["plan_hash"] != plan["plan_hash"]
        ):
            raise OnboardingError("cleanup_plan_mismatch", "Cleanup receipt is from another plan.")
        return previous
    provisioning_path = run_dir / "provisioning-receipt.json"
    if not provisioning_path.exists():
        provisioning_path = run_dir / "provisioning-state.json"
    provisioning = read_json(provisioning_path) if provisioning_path.exists() else {}
    if provisioning.get("plan_hash") and provisioning["plan_hash"] != plan["plan_hash"]:
        raise OnboardingError("journal_plan_mismatch", "Cleanup journal belongs to another plan.")
    project_payload = provisioning.get("project")
    if config.mode == "scratch":
        group_id = str(provisioning.get("resource_group_id") or "")
        if not group_id and isinstance(project_payload, dict):
            group_id = str(project_payload.get("resource_group_id") or "")
        if not group_id:
            group_id = (
                f"/subscriptions/{config.subscription_id}/resourceGroups/"
                f"{resource_group_name(config, str(plan['run_id']))}"
            )
        cleanup_scratch(
            selected_cli,
            resource_group_id=group_id,
            run_id=str(plan["run_id"]),
            owner_object_id=context.user_object_id,
        )
    else:
        if not isinstance(project_payload, dict):
            return _finish_cleanup(run_dir, plan)
        resources = ProjectResources(**project_payload)
        if config.create_sample_agent:
            if (
                not provisioning.get("agent") and config.agent_type
                and provisioning.get("agent_creation_started")
            ):
                owned = find_owned_agent(
                    project_client(resources.project_endpoint, context.tenant_id),
                    run_id=str(plan["run_id"]),
                    kind=config.agent_type,
                )
                if owned is not None:
                    provisioning["agent"] = asdict(owned)
                    provisioning["agent_created"] = True
            if provisioning.get("agent_created"):
                _cleanup_existing_sample_agent(
                    run_dir=run_dir,
                    plan=plan,
                    provisioning=provisioning,
                    resources=resources,
                    context=context,
                )
        connection_mutations = [
            item
            for item in plan.get("mutations", [])
            if isinstance(item, dict)
            and item.get("kind") == "create_app_insights_connections"
        ]
        expected_connection_names: set[str] = set()
        if len(connection_mutations) == 1:
            properties = connection_mutations[0].get("properties")
            if isinstance(properties, dict):
                expected_connection_names = {
                    str(properties.get("account_connection_name") or ""),
                    str(properties.get("project_connection_name") or ""),
                }
                expected_connection_names.discard("")
        allowed_connection_parents = {
            resources.foundry_account_resource_id.rstrip("/").casefold(),
            resources.project_resource_id.rstrip("/").casefold(),
        }
        for connection_id in reversed(list(provisioning.get("created_connection_ids") or [])):
            parsed_connection_id = str(connection_id).rstrip("/")
            connection_name = parsed_connection_id.split("/")[-1]
            connection_parent = parsed_connection_id.rsplit("/connections/", 1)[0]
            if (
                not expected_connection_names
                or connection_name not in expected_connection_names
                or connection_parent.casefold() not in allowed_connection_parents
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Connection cleanup target is not present in the frozen plan.",
                )
            live = _cleanup_resource(selected_cli, parsed_connection_id, "2025-06-01")
            if live is None:
                continue
            properties = live.get("properties") if isinstance(live, dict) else None
            metadata = (
                properties.get("metadata")
                if isinstance(properties, dict)
                else None
            )
            live_app_insights_id = (
                str(metadata.get("ResourceId") or metadata.get("resourceId") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if (
                not isinstance(properties, dict)
                or str(properties.get("category") or "").casefold() != "appinsights"
                or live_app_insights_id.rstrip("/").casefold()
                != resources.application_insights_resource_id.rstrip("/").casefold()
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Live connection no longer matches the onboarding receipt.",
                )
            selected_cli.rest(
                method="delete",
                url=(
                    f"https://management.azure.com{parsed_connection_id}"
                    "?api-version=2025-06-01"
                ),
            )
        allowed_pending_connections = {
            f"{parent}/connections/{name}".casefold()
            for parent in allowed_connection_parents
            for name in expected_connection_names
        }
        unresolved_mutations: list[str] = []
        for identifier in provisioning.get("pending_connection_ids") or []:
            if not isinstance(identifier, str) or identifier.casefold() not in (
                allowed_pending_connections
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch", "Pending connection is not in the frozen plan."
                )
            if _cleanup_resource(selected_cli, identifier, "2025-06-01") is not None:
                unresolved_mutations.append(identifier)
        planned_assignments: set[tuple[str, str, str]] = set()
        for item in plan.get("mutations", []):
            if not isinstance(item, dict):
                continue
            properties = item.get("properties")
            if not isinstance(properties, dict):
                continue
            kind = item.get("kind")
            if kind == "create_role_assignment":
                principal_id = str(properties.get("principal_id") or "")
            elif kind == "ensure_role_assignment_after_identity":
                principal = str(properties.get("principal") or "")
                principal_id = (
                    resources.project_principal_id
                    if principal == "project_system_identity"
                    else principal
                )
            else:
                continue
            role_definition_id = str(properties.get("role_definition_id") or "")
            scope = str(item.get("target") or "")
            if principal_id and role_definition_id and scope:
                planned_assignments.add(
                    (
                        principal_id.casefold(),
                        role_definition_id.casefold(),
                        scope.rstrip("/").casefold(),
                    )
                )
        roles_by_id = {
            role.definition_id.casefold(): role
            for role in (
                COGNITIVE_SERVICES_OPENAI_USER,
                FOUNDRY_USER,
                FOUNDRY_PROJECT_MANAGER,
                MONITORING_READER,
                PRIVILEGED_MONITORING_DATA_READER,
            )
        }
        allowed_pending_roles: set[str] = set()
        for principal, role_id, scope in planned_assignments:
            role = roles_by_id.get(role_id)
            if role is not None:
                required = RequiredAssignment(
                    principal, "User" if principal == context.user_object_id.casefold()
                    else "ServicePrincipal", role, scope,
                )
                allowed_pending_roles.add(
                    f"{scope}/providers/Microsoft.Authorization/"
                    f"roleAssignments/{required.assignment_id}".casefold()
                )
        for identifier in provisioning.get("pending_role_assignment_ids") or []:
            if (
                not isinstance(identifier, str)
                or identifier.casefold() not in allowed_pending_roles
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch", "Pending role assignment is not in the frozen plan."
                )
            if _cleanup_resource(selected_cli, identifier, "2022-04-01") is not None:
                unresolved_mutations.append(identifier)
        for assignment in provisioning.get("created_role_assignments") or []:
            if not isinstance(assignment, dict):
                raise OnboardingError(
                    "invalid_cleanup_receipt",
                    "Role assignment cleanup record is invalid.",
                )
            assignment_id = str(assignment.get("assignment_id") or "")
            resource_id = str(assignment.get("id") or "")
            principal_id = str(assignment.get("principal_id") or "")
            principal_type = str(assignment.get("principal_type") or "")
            role_definition_id = str(assignment.get("role_definition_id") or "")
            scope = str(assignment.get("scope") or "")
            planned_key = (
                principal_id.casefold(),
                role_definition_id.casefold(),
                scope.rstrip("/").casefold(),
            )
            if planned_key not in planned_assignments:
                raise OnboardingError(
                    "cleanup_target_mismatch", "Role assignment is not present in the frozen plan."
                )
            role = roles_by_id.get(role_definition_id.casefold())
            if principal_type not in {"User", "ServicePrincipal"} or role is None:
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Role assignment record uses an unsupported principal or role.",
                )
            required = RequiredAssignment(
                principal_id=principal_id,
                principal_type=cast(PrincipalType, principal_type),
                role=role,
                scope=scope,
            )
            if (
                required.role.definition_id.casefold()
                != role_definition_id.casefold()
                or required.assignment_id != assignment_id
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Role assignment record is not deterministic or uses an unsupported role.",
                )
            expected_resource_id = (
                f"{scope.rstrip('/')}"
                f"/providers/Microsoft.Authorization/roleAssignments/{assignment_id}"
            )
            if resource_id.rstrip("/").casefold() != expected_resource_id.casefold():
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Role assignment resource ID differs from the frozen plan.",
                )
            live = _cleanup_resource(selected_cli, resource_id, "2022-04-01")
            if live is None:
                continue
            properties = live.get("properties") if isinstance(live, dict) else None
            if (
                not isinstance(properties, dict)
                or str(properties.get("principalId") or "").casefold()
                != str(assignment.get("principal_id") or "").casefold()
                or str(properties.get("roleDefinitionId") or "").split("/")[-1].casefold()
                != str(assignment.get("role_definition_id") or "").casefold()
                or str(properties.get("scope") or "").rstrip("/").casefold()
                != str(assignment.get("scope") or "").rstrip("/").casefold()
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Live role assignment no longer matches the onboarding receipt.",
                )
            selected_cli.run(["role", "assignment", "delete", "--ids", resource_id])
        if unresolved_mutations:
            raise OnboardingError(
                "cleanup_unconfirmed_mutations",
                "Some writes have uncertain ownership. Confirmed resources were cleaned, "
                "but these resources were preserved for administrator reconciliation.",
                {"run_dir": str(run_dir), "resource_ids": unresolved_mutations},
            )
    return _finish_cleanup(run_dir, plan)
