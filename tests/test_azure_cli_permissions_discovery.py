from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from insights_onboarding import azure_cli as azure_cli_module
from insights_onboarding.azure_cli import AzureCli, CommandOutput
from insights_onboarding.discovery import (
    account_id_from_project,
    check_azure_cli_version,
    derive_project_endpoint,
    find_project_by_endpoint,
    linked_workspace_id,
    list_app_insights_connections,
    list_application_insights,
    list_model_deployments,
    list_recommended_insight_models,
    model_deployment_command,
    model_is_available,
    model_quota_available,
    parse_version,
)
from insights_onboarding.errors import OnboardingError
from insights_onboarding.permissions import (
    COGNITIVE_SERVICES_OPENAI_USER,
    FOUNDRY_USER,
    PRIVILEGED_MONITORING_DATA_READER,
    RequiredAssignment,
    action_allowed,
    create_assignment,
    missing_assignments,
    require_actions,
    required_assignments,
    role_guid,
)


class StubCli:
    def __init__(self, *, json_values=None, rest_values=None) -> None:
        self.json_values = list(json_values or [])
        self.rest_values = list(rest_values or [])
        self.json_calls: list[tuple[list[str], dict[str, object]]] = []
        self.rest_calls: list[tuple[str, str, bool]] = []

    def json(self, arguments, **kwargs):
        self.json_calls.append((list(arguments), dict(kwargs)))
        return self.json_values.pop(0)

    def rest(self, *, method: str, url: str, allow_failure: bool = False, **_kwargs):
        self.rest_calls.append((method, url, allow_failure))
        return self.rest_values.pop(0)


class EndpointDiscoveryCli:
    def __init__(self, *, project_rows: list[dict[str, object]]) -> None:
        self.project_rows = project_rows
        self.rest_body: dict[str, object] | None = None

    def account_show(self):
        return {"tenantId": "22222222-2222-2222-2222-222222222222"}

    def json(self, arguments, **_kwargs):
        assert list(arguments) == ["account", "list", "--all"]
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Demo",
                "tenantId": "22222222-2222-2222-2222-222222222222",
                "state": "Enabled",
                "isDefault": True,
            },
            {
                "id": "99999999-9999-9999-9999-999999999999",
                "name": "Other tenant",
                "tenantId": "88888888-8888-8888-8888-888888888888",
                "state": "Enabled",
                "isDefault": False,
            },
        ]

    def rest(self, *, method: str, url: str, body, **_kwargs):
        assert method == "post"
        assert "Microsoft.ResourceGraph/resources" in url
        self.rest_body = body
        return {"data": self.project_rows}


def test_project_endpoint_resolves_subscription_through_resource_graph(
    azure_ids: dict[str, str],
) -> None:
    cli = EndpointDiscoveryCli(
        project_rows=[
            {
                "id": azure_ids["project"],
                "name": "demo-account/demo-project",
                "location": "westus3",
                "subscriptionId": "11111111-1111-1111-1111-111111111111",
            }
        ]
    )

    result = find_project_by_endpoint(
        cli,
        "https://demo-account.services.ai.azure.com/api/projects/demo-project",
    )

    assert result["project_resource_id"] == azure_ids["project"]
    assert result["subscription_id"] == "11111111-1111-1111-1111-111111111111"
    assert result["account_name"] == "demo-account"
    assert cli.rest_body is not None
    assert cli.rest_body["subscriptions"] == [
        "11111111-1111-1111-1111-111111111111"
    ]


def test_project_endpoint_reports_active_tenant_miss() -> None:
    with pytest.raises(OnboardingError) as excinfo:
        find_project_by_endpoint(
            EndpointDiscoveryCli(project_rows=[]),
            "https://demo-account.services.ai.azure.com/api/projects/demo-project",
        )
    assert excinfo.value.code == "project_not_found_in_active_tenant"


def test_application_insights_discovery_can_scope_to_resource_group(
    azure_ids: dict[str, str],
) -> None:
    cli = StubCli(
        json_values=[
            [
                {
                    "id": azure_ids["app_insights"],
                    "name": "demo-appi",
                    "location": "westus3",
                }
            ]
        ]
    )

    components = list_application_insights(
        cli,
        "11111111-1111-1111-1111-111111111111",
        resource_group="rg-agent-insights",
    )

    assert components == [
        {
            "id": azure_ids["app_insights"],
            "name": "demo-appi",
            "resource_group": "rg-agent-insights",
            "location": "westus3",
        }
    ]
    arguments = cli.json_calls[0][0]
    assert arguments[-2:] == ["--resource-group", "rg-agent-insights"]


def test_azure_cli_json_successfully_parses_stdout() -> None:
    calls: list[tuple[list[str], float]] = []

    def executor(command, timeout):
        calls.append((list(command), timeout))
        return CommandOutput(0, '{"ok": true, "count": 2}', "")

    value = AzureCli(executor=executor).json(["group", "exists", "--name", "demo"], timeout=12)

    assert value == {"ok": True, "count": 2}
    assert calls == [
        (["az", "group", "exists", "--name", "demo", "--output", "json"], 12),
    ]


def test_azure_cli_rest_accepts_empty_delete_response() -> None:
    def executor(_command, _timeout):
        return CommandOutput(0, "", "")

    result = AzureCli(executor=executor).rest(
        method="delete",
        url=(
            "https://management.azure.com/subscriptions/"
            "11111111-1111-1111-1111-111111111111"
            "/resourceGroups/demo?api-version=2024-11-01"
        ),
    )

    assert result is None


def test_azure_cli_rest_uses_temporary_body_file() -> None:
    observed_path: Path | None = None

    def executor(command, _timeout):
        nonlocal observed_path
        body_argument = command[command.index("--body") + 1]
        assert body_argument.startswith("@")
        observed_path = Path(body_argument[1:])
        assert json.loads(observed_path.read_text(encoding="utf-8")) == {
            "subscriptions": ["sub-1", "sub-2"],
            "query": "Resources | take 1",
        }
        return CommandOutput(0, '{"data": []}', "")

    result = AzureCli(executor=executor).rest(
        method="post",
        url=(
            "https://management.azure.com/providers/Microsoft.ResourceGraph/resources"
            "?api-version=2022-10-01"
        ),
        body={
            "subscriptions": ["sub-1", "sub-2"],
            "query": "Resources | take 1",
        },
    )

    assert result == {"data": []}
    assert observed_path is not None
    assert not observed_path.exists()


def test_azure_cli_failure_redacts_stderr_and_sensitive_arguments() -> None:
    secret = "topsecretvalue123456"

    def executor(_command, _timeout):
        return CommandOutput(
            2,
            "",
            f"Authorization: {secret}; Bearer bearersecret1234567890; "
            "AccountKey=abcdef1234567890; "
            "https://example.test/?sig=abcdef1234567890",
        )

    cli = AzureCli(executor=executor)
    with pytest.raises(OnboardingError) as excinfo:
        cli.run(
            [
                "rest",
                "--headers",
                f"Authorization={secret}",
                "--body",
                '{"token":"abcdef1234567890"}',
                "--client-secret",
                "abcdef1234567890",
            ]
        )

    details = excinfo.value.details
    assert excinfo.value.code == "azure_cli_failed"
    assert details["command"] == [
        "az",
        "rest",
        "--headers",
        "******",
        "--body",
        "******",
        "--client-secret",
        "******",
    ]
    assert secret not in details["stderr"]
    assert "bearersecret1234567890" not in details["stderr"]
    assert "abcdef1234567890" not in details["stderr"]
    assert "******" in details["stderr"]


def test_azure_cli_invalid_json_missing_cli_and_timeout(monkeypatch) -> None:
    cli = AzureCli(executor=lambda _command, _timeout: CommandOutput(0, "{", ""))
    with pytest.raises(OnboardingError) as invalid_json:
        cli.json(["version"])
    assert invalid_json.value.code == "invalid_azure_cli_json"

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("az")

    monkeypatch.setattr(azure_cli_module.subprocess, "run", missing)
    with pytest.raises(OnboardingError) as missing_cli:
        azure_cli_module._subprocess_executor(["az"], 1)
    assert missing_cli.value.code == "azure_cli_missing"

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["az"], timeout=1)

    monkeypatch.setattr(azure_cli_module.subprocess, "run", timeout)
    with pytest.raises(OnboardingError) as timed_out:
        azure_cli_module._subprocess_executor(["az"], 1)
    assert timed_out.value.code == "azure_cli_timeout"


def test_permission_matching_and_assignment_identity(azure_ids: dict[str, str]) -> None:
    required = RequiredAssignment(
        principal_id="44444444-4444-4444-4444-444444444444",
        principal_type="ServicePrincipal",
        role=FOUNDRY_USER,
        scope=f"{azure_ids['account']}/",
    )
    same = RequiredAssignment(
        principal_id=required.principal_id.upper(),
        principal_type="ServicePrincipal",
        role=FOUNDRY_USER,
        scope=azure_ids["account"].upper(),
    )
    assert required.assignment_id == same.assignment_id
    assert role_guid(
        "/subscriptions/x/providers/Microsoft.Authorization/roleDefinitions/"
        "53ca6127-db72-4b80-b1b0-d745d6d5456d"
    ) == FOUNDRY_USER.definition_id

    permissions = [
        {
            "actions": ["Microsoft.Authorization/*/write", "Microsoft.Insights/*/read"],
            "notActions": ["Microsoft.Authorization/roleAssignments/delete"],
        }
    ]
    assert action_allowed(permissions, "Microsoft.Authorization/roleAssignments/write")
    assert not action_allowed(permissions, "Microsoft.Authorization/roleAssignments/delete")

    with pytest.raises(OnboardingError) as excinfo:
        require_actions(
            permissions,
            [
                "Microsoft.Authorization/roleAssignments/write",
                "Microsoft.Authorization/roleAssignments/delete",
            ],
            scope=azure_ids["account"],
        )
    assert excinfo.value.code == "insufficient_preflight_permission"
    assert excinfo.value.details["missing_actions"] == [
        "Microsoft.Authorization/roleAssignments/delete"
    ]


def test_project_mi_uses_narrow_model_inference_role(
    azure_ids: dict[str, str],
) -> None:
    assignments = required_assignments(
        current_user_id="33333333-3333-3333-3333-333333333333",
        project_principal_id="44444444-4444-4444-4444-444444444444",
        foundry_account_id=azure_ids["account"],
        project_id=azure_ids["project"],
        application_insights_id=azure_ids["app_insights"],
        workspace_id=azure_ids["workspace"],
        agent_type="prompt",
        protected_trace_content=False,
    )
    project_mi_account_roles = [
        item.role
        for item in assignments
        if item.principal_type == "ServicePrincipal"
        and item.scope == azure_ids["account"]
    ]

    assert project_mi_account_roles == [COGNITIVE_SERVICES_OPENAI_USER]
    assert FOUNDRY_USER not in project_mi_account_roles
    project_mi_project_roles = [
        item.role
        for item in assignments
        if item.principal_type == "ServicePrincipal"
        and item.scope == azure_ids["project"]
    ]
    assert project_mi_project_roles == [FOUNDRY_USER]
    assert all(
        item.role != PRIVILEGED_MONITORING_DATA_READER for item in assignments
    )


def test_protected_trace_content_adds_workspace_roles(
    azure_ids: dict[str, str],
) -> None:
    assignments = required_assignments(
        current_user_id="33333333-3333-3333-3333-333333333333",
        project_principal_id="44444444-4444-4444-4444-444444444444",
        foundry_account_id=azure_ids["account"],
        project_id=azure_ids["project"],
        application_insights_id=azure_ids["app_insights"],
        workspace_id=azure_ids["workspace"],
        agent_type="prompt",
        protected_trace_content=True,
    )

    privileged_assignments = [
        item
        for item in assignments
        if item.role == PRIVILEGED_MONITORING_DATA_READER
    ]
    assert {
        (item.principal_type, item.scope) for item in privileged_assignments
    } == {
        ("ServicePrincipal", azure_ids["workspace"]),
        ("User", azure_ids["workspace"]),
    }


def test_missing_assignments_uses_inherited_results_and_create_is_exact(
    azure_ids: dict[str, str],
) -> None:
    required = RequiredAssignment(
        principal_id="44444444-4444-4444-4444-444444444444",
        principal_type="User",
        role=FOUNDRY_USER,
        scope=azure_ids["project"],
    )
    cli = StubCli(
        json_values=[
            [
                {
                    "principalId": required.principal_id,
                    "roleDefinitionId": (
                        "/subscriptions/x/providers/Microsoft.Authorization/roleDefinitions/"
                        f"{required.role.definition_id}"
                    ),
                    "scope": azure_ids["account"],
                }
            ]
        ]
    )

    assert missing_assignments(cli, [required]) == []
    assert "--include-inherited" in cli.json_calls[0][0]

    exact = StubCli(
        json_values=[
            {
                "id": "/subscriptions/x/providers/Microsoft.Authorization/roleAssignments/1",
                "principalId": required.principal_id,
                "roleDefinitionId": required.role.definition_id,
                "scope": required.scope,
            }
        ]
    )
    created = create_assignment(exact, required)
    assert created["principalId"] == required.principal_id

    mismatch = StubCli(
        json_values=[
            {
                "principalId": required.principal_id,
                "roleDefinitionId": required.role.definition_id,
                "scope": azure_ids["account"],
            }
        ]
    )
    with pytest.raises(OnboardingError) as bad_scope:
        create_assignment(mismatch, required)
    assert bad_scope.value.code == "role_assignment_mismatch"


def test_discovery_helpers_parse_versions_connections_and_models(
    azure_ids: dict[str, str],
) -> None:
    assert parse_version("azure-cli                         2.80.1") == (2, 80, 1)
    assert derive_project_endpoint(azure_ids["project"]) == (
        "https://demo-account.services.ai.azure.com/api/projects/demo-project"
    )
    assert account_id_from_project(azure_ids["project"]) == azure_ids["account"]

    version_cli = StubCli(json_values=[{"azure-cli": "2.80.1"}])
    assert check_azure_cli_version(version_cli) == "2.80.1"

    stale_cli = StubCli(json_values=[{"azure-cli": "2.79.9"}])
    with pytest.raises(OnboardingError) as stale:
        check_azure_cli_version(stale_cli)
    assert stale.value.code == "azure_cli_too_old"

    connections_cli = StubCli(
        rest_values=[
            {
                "value": [
                    {
                        "id": "conn-1",
                        "name": "appi-1",
                        "properties": {
                            "category": "AppInsights",
                            "metadata": {"ResourceId": azure_ids["app_insights"]},
                        },
                    },
                    {
                        "id": "conn-2",
                        "name": "appi-2",
                        "properties": {
                            "category": "AppInsights",
                            "metadata": {"resourceId": azure_ids["app_insights"]},
                        },
                    },
                    {"id": "skip-me", "properties": {"category": "Storage"}},
                ]
            }
        ]
    )
    parsed = list_app_insights_connections(connections_cli, azure_ids["project"])
    assert [item["id"] for item in parsed] == ["conn-1", "conn-2"]
    assert parsed[0]["resource_id"] == azure_ids["app_insights"]

    workspace = linked_workspace_id(
        {"properties": {"workspaceResourceId": azure_ids["workspace"]}}
    )
    assert workspace == azure_ids["workspace"]

    model_cli = StubCli(
        json_values=[
            [
                {
                    "model": {
                        "name": "gpt-4.1-mini",
                        "version": "2025-04-14",
                        "format": "OpenAI",
                    },
                    "skus": [{"name": "GlobalStandard"}],
                },
                {
                    "name": "other-model",
                    "version": "1",
                    "format": "OpenAI",
                },
            ]
        ]
    )
    assert model_is_available(
        model_cli,
        location="westus3",
        model_name="gpt-4.1-mini",
        model_version="2025-04-14",
        model_format="OpenAI",
        sku_name="GlobalStandard",
    )
    assert not model_is_available(
        StubCli(
            json_values=[
                [
                    {
                        "model": {
                            "name": "gpt-4.1-mini",
                            "version": "2025-04-14",
                            "format": "OpenAI",
                        },
                        "skus": [{"name": "Standard"}],
                    }
                ]
            ]
        ),
        location="westus3",
        model_name="gpt-4.1-mini",
        model_version="2025-04-14",
        model_format="OpenAI",
        sku_name="GlobalStandard",
    )


def test_model_catalog_rejects_deprecated_or_expired_models() -> None:
    base_model = {
        "name": "gpt-test",
        "version": "1",
        "format": "OpenAI",
        "capabilities": {
            "chatCompletion": "true",
            "responses": "true",
        },
        "skus": [{"name": "GlobalStandard"}],
    }
    deprecated = {**base_model, "lifecycleStatus": "Deprecated"}
    expired = {
        **base_model,
        "lifecycleStatus": "GenerallyAvailable",
        "deprecation": {"inference": "2020-01-01T00:00:00Z"},
    }
    current = {
        **base_model,
        "lifecycleStatus": "GenerallyAvailable",
        "deprecation": {"inference": "2099-01-01T00:00:00Z"},
    }

    for model, expected in (
        (deprecated, False),
        (expired, False),
        (current, True),
    ):
        assert (
            model_is_available(
                StubCli(json_values=[[{"model": model}]]),
                location="westus3",
                model_name="gpt-test",
                model_version="1",
                model_format="OpenAI",
                sku_name="GlobalStandard",
            )
            is expected
        )


def test_model_quota_requires_account_and_model_headroom() -> None:
    available = [
        {
            "name": {"value": "OpenAI.S0.AccountCount"},
            "currentValue": 1,
            "limit": 3,
        },
        {
            "name": {"value": "OpenAI.GlobalStandard.gpt4.1-mini"},
            "currentValue": 10,
            "limit": 20,
        },
    ]
    assert model_quota_available(
        StubCli(json_values=[available]),
        location="westus3",
        model_name="gpt-4.1-mini",
        model_format="OpenAI",
        sku_name="GlobalStandard",
        capacity=1,
    )

    exhausted = [
        available[0],
        {
            "name": {"value": "OpenAI.GlobalStandard.gpt4.1-mini"},
            "currentValue": 20,
            "limit": 20,
        },
    ]
    assert not model_quota_available(
        StubCli(json_values=[exhausted]),
        location="westus3",
        model_name="gpt-4.1-mini",
        model_format="OpenAI",
        sku_name="GlobalStandard",
        capacity=1,
    )


def test_recommended_models_prioritize_terra_and_exclude_legacy() -> None:
    def model(
        name: str,
        version: str,
        *,
        lifecycle: str = "GenerallyAvailable",
    ) -> dict[str, object]:
        usage_name = f"OpenAI.GlobalStandard.{name}"
        return {
            "model": {
                "name": name,
                "version": version,
                "format": "OpenAI",
                "lifecycleStatus": lifecycle,
                "capabilities": {
                    "chatCompletion": "true",
                    "responses": "true",
                },
                "skus": [
                    {
                        "name": "GlobalStandard",
                        "usageName": usage_name,
                    }
                ],
            }
        }

    catalog = [
        model("gpt-4.1", "1"),
        model("gpt-5.4", "1"),
        model("gpt-5.6-terra", "2"),
        model("gpt-5.5", "1", lifecycle="Deprecated"),
    ]
    usage = [
        {
            "name": {"value": "OpenAI.S0.AccountCount"},
            "currentValue": 1,
            "limit": 30,
        },
        {
            "name": {"value": "OpenAI.GlobalStandard.gpt-5.4"},
            "currentValue": 0,
            "limit": 100,
        },
        {
            "name": {"value": "OpenAI.GlobalStandard.gpt-5.6-terra"},
            "currentValue": 10,
            "limit": 100,
        },
    ]

    result = list_recommended_insight_models(
        StubCli(json_values=[catalog, usage]),
        location="westus3",
        minimum_capacity=10,
    )

    assert [item["name"] for item in result] == ["gpt-5.6-terra", "gpt-5.4"]
    assert result[0]["quality_tier"] == "preferred_customer_candidate"
    assert result[1]["quality_tier"] == "service_regression_baseline"

    expired = catalog[2]
    expired["model"]["deprecation"] = {"inference": "2020-01-01T00:00:00Z"}
    assert list_recommended_insight_models(
        StubCli(json_values=[[expired], usage]),
        location="westus3",
        minimum_capacity=10,
    ) == []

    exhausted_account = [
        {
            "name": {"value": "OpenAI.S0.AccountCount"},
            "currentValue": 30,
            "limit": 30,
        },
        *usage[1:],
    ]
    assert list_recommended_insight_models(
        StubCli(json_values=[catalog[:2], exhausted_account]),
        location="westus3",
        minimum_capacity=10,
    ) == []


def test_deployment_discovery_and_command_are_project_scoped(
    azure_ids: dict[str, str],
) -> None:
    deployments = list_model_deployments(
        StubCli(
            json_values=[
                [
                    {
                        "name": "insights-terra",
                        "properties": {
                            "model": {
                                "name": "gpt-5.6-terra",
                                "version": "2",
                                "format": "OpenAI",
                            }
                        },
                        "sku": {"name": "GlobalStandard", "capacity": 10},
                    },
                    {
                        "name": "legacy",
                        "properties": {
                            "model": {
                                "name": "gpt-4.1",
                                "version": "1",
                                "format": "OpenAI",
                            }
                        },
                        "sku": {"name": "GlobalStandard", "capacity": 10},
                    },
                ]
            ]
        ),
        azure_ids["project"],
    )
    assert deployments[0]["deployment_name"] == "insights-terra"
    assert deployments[0]["gpt5_plus"] is True
    assert deployments[1]["gpt5_plus"] is False

    command = model_deployment_command(
        azure_ids["project"],
        deployment_name="agent-insights-terra",
        model_name="gpt-5.6-terra",
        model_version="2",
        model_format="OpenAI",
        sku_name="GlobalStandard",
        capacity=10,
    )
    assert command[:5] == [
        "az",
        "cognitiveservices",
        "account",
        "deployment",
        "create",
    ]
    assert command[command.index("--name") + 1] == "demo-account"
    assert command[command.index("--resource-group") + 1] == "rg-agent-insights"
    assert command[-2:] == ["--sku-capacity", "10"]
