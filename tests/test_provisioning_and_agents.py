from __future__ import annotations

from types import SimpleNamespace

import pytest
from insights_onboarding import agents, provisioning
from insights_onboarding.errors import OnboardingError


class ScratchCli:
    def __init__(self, outputs: dict[str, object], *, group_exists: bool = False) -> None:
        self.outputs = outputs
        self.group_exists = group_exists
        self.calls: list[list[str]] = []

    def json(self, arguments, **_kwargs):
        arguments = list(arguments)
        self.calls.append(arguments)
        if arguments[:2] == ["group", "exists"]:
            return self.group_exists
        if arguments[:2] in (["group", "create"], ["group", "show"]):
            return {
                "id": (
                    "/subscriptions/11111111-1111-1111-1111-111111111111"
                    "/resourceGroups/rg-agent-insights-abc123def456"
                ),
                "tags": {
                    "created-by": "agent-insights-quickstart",
                    "run-id": "abc123def456",
                    "owner-object-id": "33333333-3333-3333-3333-333333333333",
                },
            }
        if arguments[:3] == ["deployment", "group", "create"]:
            return {
                "properties": {
                    "outputs": {
                        name: {"type": "string", "value": value}
                        for name, value in self.outputs.items()
                    }
                }
            }
        if arguments[:3] == ["deployment", "sub", "create"]:
            return {
                "properties": {
                    "outputs": {
                        "projectConnectionId": {
                            "type": "string",
                            "value": "project-connection",
                        },
                    }
                }
            }
        if arguments[:2] == ["resource", "update"]:
            return {
                "identity": {
                    "principalId": "44444444-4444-4444-4444-444444444444"
                }
            }
        raise AssertionError(arguments)


class ConnectionCli:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)

    def rest(self, **_kwargs):
        return self.responses.pop(0)


def _connection(
    *,
    connection_id: str,
    name: str,
    category: str,
    resource_id: str,
) -> dict[str, object]:
    return {
        "id": connection_id,
        "name": name,
        "properties": {
            "category": category,
            "metadata": {"ResourceId": resource_id},
        },
    }


def test_provision_scratch_maps_nonsecret_outputs(
    make_config,
    make_resources,
    azure_context,
    run_id,
) -> None:
    expected = make_resources()
    cli = ScratchCli(
        {
            "foundryAccountId": expected.foundry_account_resource_id,
            "foundryProjectId": expected.project_resource_id,
            "foundryProjectEndpoint": expected.project_endpoint,
            "foundryProjectPrincipalId": expected.project_principal_id,
            "logAnalyticsWorkspaceId": expected.log_analytics_workspace_resource_id,
            "applicationInsightsId": expected.application_insights_resource_id,
            "modelDeploymentName": expected.model_deployment_name,
        }
    )

    actual = provisioning.provision_scratch(
        cli,
        config=make_config(),
        context=azure_context,
        run_id=run_id,
    )

    assert actual.project_resource_id == expected.project_resource_id
    assert actual.project_endpoint == expected.project_endpoint
    assert actual.resource_group_id is not None
    assert actual.resource_group_id.endswith(
        "/resourceGroups/rg-agent-insights-abc123def456"
    )
    deployment_call = next(
        call for call in cli.calls if call[:3] == ["deployment", "group", "create"]
    )
    assert "--template-file" in deployment_call
    assert not any("connectionstring" in item.casefold() for item in deployment_call)


def test_provision_scratch_rejects_missing_outputs(
    make_config,
    azure_context,
    run_id,
) -> None:
    cli = ScratchCli({})

    with pytest.raises(OnboardingError) as excinfo:
        provisioning.provision_scratch(
            cli,
            config=make_config(),
            context=azure_context,
            run_id=run_id,
        )

    assert excinfo.value.code == "missing_deployment_outputs"


def test_existing_identity_and_connection_creation(
    monkeypatch,
    make_resources,
    azure_ids,
    run_id,
) -> None:
    cli = ScratchCli({})
    monkeypatch.setattr(
        provisioning,
        "get_project",
        lambda *_args: {"identity": {"type": "SystemAssigned"}},
    )
    principal_id = provisioning.ensure_project_identity(
        cli,
        project_resource_id=azure_ids["project"],
    )
    assert principal_id == "44444444-4444-4444-4444-444444444444"

    monkeypatch.setattr(
        provisioning,
        "list_app_insights_connections",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        provisioning,
        "plan_existing_connections",
        lambda *_args, **_kwargs: {
            "project_connection_name": "agent-insights-test",
        },
    )
    resources = make_resources()
    connection_ids = provisioning.ensure_existing_connections(
        cli,
        project_resource_id=resources.project_resource_id,
        application_insights_resource_id=resources.application_insights_resource_id,
        location="westus3",
        run_id=run_id,
    )
    assert connection_ids == ("project-connection",)


def test_existing_connection_creation_reuses_current_connection(
    monkeypatch,
    make_resources,
    run_id,
) -> None:
    monkeypatch.setattr(
        provisioning,
        "list_app_insights_connections",
        lambda *_args: [{"id": "existing", "resource_id": "resource"}],
    )

    assert (
        provisioning.ensure_existing_connections(
            object(),
            project_resource_id=make_resources().project_resource_id,
            application_insights_resource_id=
                make_resources().application_insights_resource_id,
            location="westus3",
            run_id=run_id,
        )
        == ()
    )


def test_connection_plan_creates_one_project_scoped_connection(
    make_resources,
) -> None:
    resources = make_resources()
    cli = ConnectionCli([{"value": []}])

    plan = provisioning.plan_existing_connections(
        cli,
        project_resource_id=resources.project_resource_id,
        application_insights_resource_id=resources.application_insights_resource_id,
    )

    assert set(plan) == {"project_connection_name"}
    assert str(plan["project_connection_name"]).startswith("agent-insights-")


def test_connection_plan_rejects_project_name_collision(
    make_resources,
) -> None:
    resources = make_resources()
    empty_plan = provisioning.plan_existing_connections(
        ConnectionCli([{"value": []}]),
        project_resource_id=resources.project_resource_id,
        application_insights_resource_id=resources.application_insights_resource_id,
    )
    connection_name = str(empty_plan["project_connection_name"])
    conflicting_id = (
        f"{resources.project_resource_id}/connections/{connection_name}"
    )
    cli = ConnectionCli(
        [
            {
                "value": [
                    _connection(
                        connection_id=conflicting_id,
                        name=connection_name,
                        category="CustomKeys",
                        resource_id="/subscriptions/other",
                    )
                ]
            }
        ]
    )

    with pytest.raises(OnboardingError) as excinfo:
        provisioning.plan_existing_connections(
            cli,
            project_resource_id=resources.project_resource_id,
            application_insights_resource_id=
                resources.application_insights_resource_id,
        )
    assert excinfo.value.code == "project_connection_name_conflict"


class FakeAgentOperations:
    def __init__(self, versions=None) -> None:
        self.versions = list(versions or [])
        self.created: list[dict[str, object]] = []

    def list_versions(self, *_args, **_kwargs):
        return self.versions

    def create_version(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(version="7")

    def create_version_from_code(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(version="8")


def test_create_prompt_and_hosted_sample_agents(
    monkeypatch,
    run_id,
) -> None:
    prompt_operations = FakeAgentOperations()
    prompt_project = SimpleNamespace(agents=prompt_operations)
    monkeypatch.setattr(agents, "_prompt_definition", lambda _model: object())

    prompt = agents.create_sample_agent(
        prompt_project,
        run_id=run_id,
        agent_type="prompt",
        model="model",
    )
    assert prompt.version == "7"
    assert prompt_operations.created[0]["metadata"][
        "agent_insights_quickstart_run_id"
    ] == run_id

    hosted_operations = FakeAgentOperations()
    hosted_project = SimpleNamespace(agents=hosted_operations)
    monkeypatch.setattr(agents, "_hosted_definition", lambda _model: object())
    monkeypatch.setattr(agents, "_wait_active", lambda *_args, **_kwargs: object())

    hosted = agents.create_sample_agent(
        hosted_project,
        run_id=run_id,
        agent_type="hosted",
        model="model",
    )
    assert hosted.version == "8"
    assert hosted.artifact_sha256
    code = hosted_operations.created[0]["code"]
    assert code[0] == "hosted-agent.zip"
    assert code[2] == "application/zip"


def test_agent_name_uses_full_run_identity(run_id) -> None:
    assert agents.agent_name(run_id, "prompt") == "insights-prompt-abc123def456"


def test_create_sample_agent_reuses_only_exact_owned_version(run_id) -> None:
    definition = SimpleNamespace(kind="prompt")
    owned = SimpleNamespace(
        version="3",
        definition=definition,
        metadata={
            "agent_insights_quickstart_owner": "agent-insights-quickstart",
            "agent_insights_quickstart_run_id": run_id,
        },
    )
    project = SimpleNamespace(agents=FakeAgentOperations([owned]))

    deployment = agents.create_sample_agent(
        project,
        run_id=run_id,
        agent_type="prompt",
        model="model",
    )
    assert deployment.version == "3"

    unowned = SimpleNamespace(version="1", definition=definition, metadata={})
    project = SimpleNamespace(agents=FakeAgentOperations([unowned]))
    with pytest.raises(OnboardingError) as excinfo:
        agents.create_sample_agent(
            project,
            run_id=run_id,
            agent_type="prompt",
            model="model",
        )
    assert excinfo.value.code == "agent_ownership_mismatch"
