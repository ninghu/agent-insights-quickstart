from __future__ import annotations

import argparse
from types import SimpleNamespace

import httpx
import pytest
from insights_onboarding import cli as cli_module
from insights_onboarding import insights_api
from insights_onboarding.errors import OnboardingError
from insights_onboarding.insights_api import AgentInsightsClient
from insights_onboarding.provisioning import cleanup_scratch

REAL_HTTPX_CLIENT = httpx.Client


class StubCli:
    def __init__(self, group_response) -> None:
        self.group_response = group_response
        self.run_calls: list[tuple[list[str], dict[str, object]]] = []

    def json(self, arguments, **_kwargs):
        assert list(arguments)[:2] == ["group", "show"]
        return self.group_response

    def run(self, arguments, **kwargs):
        self.run_calls.append((list(arguments), dict(kwargs)))


class FakeCredential:
    def get_token(self, *_scopes):
        return SimpleNamespace(token="real-secret-token")


def _patch_client(monkeypatch, handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_HTTPX_CLIENT(*args, **kwargs)

    monkeypatch.setattr(insights_api.httpx, "Client", factory)


def _config_namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "mode": "scratch",
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "location": "westus3",
        "agent_type": "prompt",
        "name_prefix": "agent-insights",
        "project_resource_id": None,
        "project_endpoint": None,
        "application_insights_resource_id": None,
        "agent_name": None,
        "model_deployment_name": None,
        "model_name": "gpt-5.4",
        "model_version": "2026-03-05",
        "model_format": "OpenAI",
        "model_sku": "GlobalStandard",
        "model_capacity": 30,
        "lookback_hours": 168,
        "create_sample_agent": False,
        "invoke_existing_agent": False,
        "enable_existing_monitor": False,
        "protected_trace_content": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_agent_insights_client_authenticates_and_validates_payloads(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer real-secret-token"
        assert request.url.params["api-version"] == "2025-05-15-preview"
        return httpx.Response(200, json={"data": []})

    _patch_client(monkeypatch, handler)

    with AgentInsightsClient(
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        credential=FakeCredential(),
    ) as client:
        assert client.list_monitors("agent") == []

    def invalid_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    _patch_client(monkeypatch, invalid_handler)
    with AgentInsightsClient(
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        credential=FakeCredential(),
    ) as client, pytest.raises(OnboardingError) as excinfo:
        client.list_monitors("agent")
    assert excinfo.value.code == "invalid_monitor_list"


def test_agent_insights_client_can_request_detailed_fixes(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["include_details"] == "true"
        return httpx.Response(200, json={"data": []})

    _patch_client(monkeypatch, handler)
    with AgentInsightsClient(
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        credential=FakeCredential(),
    ) as client:
        assert client.list_insights("monitor", include_details=True) == []


def test_agent_insights_probe_status_mapping_and_errors(monkeypatch) -> None:
    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "denied"}, headers={"request-id": "req-403"})

    _patch_client(monkeypatch, forbidden)
    with AgentInsightsClient(
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        credential=FakeCredential(),
    ) as client:
        assert client.probe() == {"reachable": True, "authorized": False}

    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": "missing"},
            headers={"x-ms-request-id": "req-404"},
        )

    _patch_client(monkeypatch, unavailable)
    with AgentInsightsClient(
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        credential=FakeCredential(),
    ) as client, pytest.raises(OnboardingError) as excinfo:
        client.probe()
    assert excinfo.value.code == "feature_unavailable"

    def failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"}, headers={"x-ms-request-id": "req-500"})

    _patch_client(monkeypatch, failure)
    with AgentInsightsClient(
        project_endpoint="https://demo.services.ai.azure.com/api/projects/demo",
        credential=FakeCredential(),
    ) as client, pytest.raises(OnboardingError) as error:
        client.get_monitor("monitor-1")
    assert error.value.details["request_id"] == "req-500"
    assert "real-secret-token" not in repr(error.value.details)


def test_agent_insights_wait_run_maps_terminal_status_and_timeout(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, payloads):
            self.payloads = list(payloads)

        def _request(self, *_args, **_kwargs):
            return 200, self.payloads.pop(0)

    failed = AgentInsightsClient.__new__(AgentInsightsClient)
    failed._request = FakeClient([{"status": "failed", "error": {"code": "BadThing"}}])._request
    with pytest.raises(OnboardingError) as excinfo:
        failed.wait_run(monitor_id="mon", run_id="run")
    assert excinfo.value.code == "insights_run_failed"
    assert excinfo.value.details == {"run_id": "run", "service_error_code": "BadThing"}

    timeout_client = AgentInsightsClient.__new__(AgentInsightsClient)
    timeout_client._request = FakeClient([{"status": "running"}, {"status": "running"}])._request
    values = iter([0.0, 1.0])
    monkeypatch.setattr(insights_api.time, "monotonic", lambda: next(values))
    monkeypatch.setattr(insights_api.time, "sleep", lambda _seconds: None)
    with pytest.raises(OnboardingError) as timeout:
        timeout_client.wait_run(monitor_id="mon", run_id="run", timeout_seconds=0.5)
    assert timeout.value.code == "insights_run_timeout"


def test_cleanup_scratch_refuses_unowned_resource_groups(azure_context: object) -> None:
    resource_group_id = (
        "/subscriptions/11111111-1111-1111-1111-111111111111"
        "/resourceGroups/rg-agent-insights"
    )
    cli = StubCli(
        {
            "id": resource_group_id,
            "tags": {
                "created-by": "agent-insights-quickstart",
                "run-id": "someone-else",
                "owner-object-id": "33333333-3333-3333-3333-333333333333",
            },
        }
    )

    with pytest.raises(OnboardingError) as excinfo:
        cleanup_scratch(
            cli,
            resource_group_id=resource_group_id,
            run_id="abc123def456",
            owner_object_id="33333333-3333-3333-3333-333333333333",
        )

    assert excinfo.value.code == "ownership_mismatch"
    assert cli.run_calls == []


def test_cli_configuration_and_timeout_validation(tmp_path) -> None:
    required_arguments = [
        "doctor",
        "--mode",
        "scratch",
        "--subscription-id",
        "00000000-0000-0000-0000-000000000000",
        "--location",
        "westus3",
        "--agent-type",
        "prompt",
    ]
    assert cli_module.parse_args(required_arguments).protected_trace_content is False
    assert (
        cli_module.parse_args(
            [*required_arguments, "--protected-trace-content"]
        ).protected_trace_content
        is True
    )
    assert cli_module._config(_config_namespace()).protected_trace_content is False
    assert (
        cli_module._config(
            _config_namespace(protected_trace_content=True)
        ).protected_trace_content
        is True
    )

    with pytest.raises(OnboardingError) as model_capacity:
        cli_module._config(_config_namespace(model_capacity=0))
    assert model_capacity.value.code == "invalid_model_capacity"

    with pytest.raises(OnboardingError) as lookback:
        cli_module._config(_config_namespace(lookback_hours=2))
    assert lookback.value.code == "invalid_lookback"

    with pytest.raises(OnboardingError) as scratch_missing:
        cli_module._config(_config_namespace(location=None, agent_type=None))
    assert scratch_missing.value.code == "incomplete_scratch_configuration"

    with pytest.raises(OnboardingError) as existing_missing:
        cli_module._config(_config_namespace(mode="existing", location=None, agent_type="hosted"))
    assert existing_missing.value.code == "incomplete_existing_configuration"
    assert existing_missing.value.details["missing"] == [
        "--project-resource-id",
        "--model-deployment-name",
        "--agent-name",
    ]

    created_agent = cli_module._config(
        _config_namespace(
            mode="existing",
            location=None,
            project_resource_id="/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.CognitiveServices/accounts/account/projects/project",
            model_deployment_name="gpt-5.4",
            create_sample_agent=True,
        )
    )
    assert created_agent.agent_name is None
    assert created_agent.create_sample_agent is True
    parsed_created_agent = cli_module.parse_args(
        [
            "doctor",
            "--mode",
            "existing",
            "--subscription-id",
            "00000000-0000-0000-0000-000000000000",
            "--project-resource-id",
            "project",
            "--agent-type",
            "prompt",
            "--model-deployment-name",
            "gpt-5.4",
            "--create-sample-agent",
        ]
    )
    assert parsed_created_agent.create_sample_agent is True

    with pytest.raises(OnboardingError) as conflicting_agent:
        cli_module._config(
            _config_namespace(
                mode="existing",
                location=None,
                project_resource_id="project",
                model_deployment_name="gpt-5.4",
                agent_name="existing-agent",
                create_sample_agent=True,
            )
        )
    assert conflicting_agent.value.code == "conflicting_agent_selection"

    assert (
        cli_module._config(
            _config_namespace(
                mode="existing",
                location=None,
                project_resource_id="project",
                model_deployment_name="gpt-5.4",
                create_sample_agent=True,
                invoke_existing_agent=True,
            )
        ).invoke_existing_agent
        is False
    )

    with pytest.raises(OnboardingError) as invalid_timeout:
        cli_module.main(
            [
                "status",
                "--run-dir",
                str(tmp_path),
                "--ingestion-timeout-seconds",
                "-1",
            ]
        )
    assert invalid_timeout.value.code == "invalid_timeout"
