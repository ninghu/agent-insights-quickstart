from __future__ import annotations

from dataclasses import asdict

import pytest
from insights_onboarding import orchestrator
from insights_onboarding.models import MonitorOutcome, TrafficOutcome
from insights_onboarding.permissions import MONITORING_READER, RequiredAssignment
from insights_onboarding.receipts import read_json


class FakeInsights:
    authorized = True

    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def probe(self):
        return {"reachable": True, "authorized": self.authorized}


def test_workspace_root_follows_current_git_repository(tmp_path) -> None:
    project = tmp_path / "customer-project"
    nested = project / "src" / "feature"
    nested.mkdir(parents=True)
    (project / ".git").mkdir()

    assert orchestrator._find_workspace_root(nested) == project
    assert orchestrator._find_workspace_root(tmp_path / "standalone") == (
        tmp_path / "standalone"
    )


def _patch_common_doctor(monkeypatch, azure_context) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_check_python_and_packages",
        lambda: {"azure-ai-projects": "2.3.0"},
    )
    monkeypatch.setattr(orchestrator, "check_azure_cli_version", lambda _cli: "2.96.0")
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    monkeypatch.setattr(
        orchestrator,
        "_require_registered_providers",
        lambda _cli: {
            "Microsoft.CognitiveServices": "Registered",
            "Microsoft.Insights": "Registered",
            "Microsoft.OperationalInsights": "Registered",
        },
    )


def test_role_preflight_returns_exact_admin_handoff(
    monkeypatch,
    azure_ids,
) -> None:
    assignment = RequiredAssignment(
        principal_id="44444444-4444-4444-4444-444444444444",
        principal_type="ServicePrincipal",
        role=MONITORING_READER,
        scope=azure_ids["app_insights"],
    )
    monkeypatch.setattr(
        orchestrator,
        "permissions_at_scope",
        lambda *_args: [{"actions": ["*/read"], "notActions": []}],
    )

    with pytest.raises(orchestrator.OnboardingError) as excinfo:
        orchestrator._require_assignment_write(
            object(),
            {azure_ids["app_insights"]},
            [assignment],
        )

    assert excinfo.value.code == "insufficient_preflight_permission"
    handoff = excinfo.value.details["admin_handoff"]
    assert handoff["verification"] == "Rerun doctor and require status=ready."
    item = handoff["role_assignments"][0]
    assert item["principal_id"] == assignment.principal_id
    assert item["role_definition_id"] == MONITORING_READER.definition_id
    assert item["scope"] == azure_ids["app_insights"]
    assert item["command"][-2:] == ["--name", assignment.assignment_id]


def test_doctor_scratch_checks_permissions_and_model(
    monkeypatch,
    make_config,
    azure_context,
) -> None:
    _patch_common_doctor(monkeypatch, azure_context)
    permission_checks: list[tuple[object, object]] = []
    monkeypatch.setattr(
        orchestrator,
        "subscription_permissions",
        lambda _cli, _subscription_id: [{"actions": ["*"], "notActions": []}],
    )
    monkeypatch.setattr(
        orchestrator,
        "require_actions",
        lambda permissions, actions, **_kwargs: permission_checks.append(
            (permissions, actions)
        ),
    )
    monkeypatch.setattr(orchestrator, "model_is_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        orchestrator,
        "model_quota_available",
        lambda *_args, **_kwargs: True,
    )

    result = orchestrator.doctor(make_config(), cli=object())

    assert result["status"] == "ready"
    assert result["scratch"]["agent_type"] == "prompt"
    assert permission_checks

    monkeypatch.setattr(orchestrator, "model_is_available", lambda *_args, **_kwargs: False)
    with pytest.raises(orchestrator.OnboardingError) as excinfo:
        orchestrator.doctor(make_config(), cli=object())
    assert excinfo.value.code == "model_unavailable"

    monkeypatch.setattr(orchestrator, "model_is_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        orchestrator,
        "model_quota_available",
        lambda *_args, **_kwargs: False,
    )
    with pytest.raises(orchestrator.OnboardingError) as quota:
        orchestrator.doctor(make_config(), cli=object())
    assert quota.value.code == "model_quota_unavailable"


def test_doctor_existing_accepts_repairable_data_plane_denial(
    monkeypatch,
    make_config,
    make_resources,
    azure_context,
    azure_ids,
) -> None:
    _patch_common_doctor(monkeypatch, azure_context)
    resources = make_resources()
    monkeypatch.setattr(orchestrator, "resolve_existing", lambda *_args, **_kwargs: resources)
    monkeypatch.setattr(
        orchestrator,
        "get_project",
        lambda *_args, **_kwargs: {"location": "westus3"},
    )
    monkeypatch.setattr(
        orchestrator,
        "list_app_insights_connections",
        lambda *_args, **_kwargs: [{"id": "connection", "resource_id": azure_ids["app_insights"]}],
    )
    monkeypatch.setattr(orchestrator, "missing_assignments", lambda *_args: [])
    monkeypatch.setattr(orchestrator, "_require_assignment_write", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_validate_existing_model", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_credential", lambda _context: object())
    monkeypatch.setattr(
        orchestrator,
        "can_query_agent_traces",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(orchestrator, "AgentInsightsClient", FakeInsights)
    FakeInsights.authorized = False
    config = make_config(
        mode="existing",
        location=None,
        agent_type="prompt",
        project_resource_id=azure_ids["project"],
        agent_name="existing-agent",
        model_deployment_name="model",
        application_insights_resource_id=azure_ids["app_insights"],
    )

    result = orchestrator.doctor(config, cli=object())

    assert result["existing"]["feature"]["reachable"] is True
    assert result["existing"]["feature"]["authorized"] is False
    assert result["existing"]["trace_check"]["status"] == (
        "deferred_until_caller_access_is_ready"
    )


def test_doctor_existing_checks_traces_without_invoking_agent(
    monkeypatch,
    make_config,
    make_resources,
    make_deployment,
    azure_context,
    azure_ids,
) -> None:
    _patch_common_doctor(monkeypatch, azure_context)
    resources = make_resources()
    deployment = make_deployment(name="existing-agent", kind="prompt")
    monkeypatch.setattr(orchestrator, "resolve_existing", lambda *_args, **_kwargs: resources)
    monkeypatch.setattr(
        orchestrator,
        "get_project",
        lambda *_args, **_kwargs: {"location": "westus3"},
    )
    monkeypatch.setattr(
        orchestrator,
        "list_app_insights_connections",
        lambda *_args, **_kwargs: [
            {"id": "connection", "resource_id": azure_ids["app_insights"]}
        ],
    )
    monkeypatch.setattr(orchestrator, "missing_assignments", lambda *_args: [])
    monkeypatch.setattr(orchestrator, "_require_assignment_write", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_validate_existing_model", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_credential", lambda _context: object())
    monkeypatch.setattr(
        orchestrator,
        "_existing_caller_capabilities",
        lambda **_kwargs: (True, True),
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_existing_agent",
        lambda *_args, **_kwargs: deployment,
    )
    trace_checks: list[dict[str, object]] = []

    def check_traces(**kwargs):
        trace_checks.append(kwargs)
        return [
            {"trace_id": "trace-1"},
            {"trace_id": "trace-2"},
            {"trace_id": "trace-3"},
        ]

    monkeypatch.setattr(orchestrator, "require_recent_agent_roots", check_traces)
    config = make_config(
        mode="existing",
        location=None,
        agent_type="prompt",
        project_resource_id=azure_ids["project"],
        agent_name="existing-agent",
        model_deployment_name="model",
        application_insights_resource_id=azure_ids["app_insights"],
    )

    result = orchestrator.doctor(config, cli=object())

    assert len(trace_checks) == 1
    assert trace_checks[0]["deployment"] == deployment
    assert result["existing"]["trace_check"] == {
        "status": "ready",
        "observed_agent_roots": 3,
        "lookback_hours": 168,
    }

    monkeypatch.setattr(
        orchestrator,
        "require_recent_agent_roots",
        lambda **_kwargs: (_ for _ in ()).throw(
            orchestrator.OnboardingError(
                "insufficient_recent_traces",
                "Run normal application traffic, then rerun doctor.",
            )
        ),
    )
    with pytest.raises(orchestrator.OnboardingError) as no_traces:
        orchestrator.doctor(config, cli=object())
    assert no_traces.value.code == "insufficient_recent_traces"


def test_onboard_scratch_writes_resumable_receipts(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    make_deployment,
    azure_context,
    run_id,
) -> None:
    config = make_config()
    resources = make_resources()
    deployment = make_deployment(name="insights-prompt-abc123de", version="1")
    outcomes = [
        TrafficOutcome(
            scenario=f"scenario-{index}",
            expected_fault=index > 6,
            response_id=f"resp-{index}",
            session_id=None,
            trace_id=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
        )
        for index in range(1, 12)
    ]
    monitor = MonitorOutcome(
        monitor_id="monitor-1",
        run_id="run-1",
        insight_ids=("insight-1",),
        estimated_cost={"amount": 0.01, "currency": "USD"},
        enabled=True,
    )
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_args, **_kwargs: {"status": "ready"})
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    monkeypatch.setattr(
        orchestrator,
        "provision_scratch",
        lambda *_args, **_kwargs: resources,
    )
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(orchestrator, "project_client", lambda *_args: object())
    monkeypatch.setattr(
        orchestrator,
        "create_sample_agent",
        lambda *_args, **_kwargs: deployment,
    )
    monkeypatch.setattr(
        orchestrator,
        "generate_sample_traffic",
        lambda *_args, **_kwargs: outcomes,
    )
    monkeypatch.setattr(orchestrator, "_credential", lambda _context: object())
    monkeypatch.setattr(
        orchestrator,
        "wait_for_ingestion",
        lambda **_kwargs: [
            {"scenario": item.scenario, "trace_id": f"trace-{index}"}
            for index, item in enumerate(outcomes, start=1)
        ],
    )
    monkeypatch.setattr(orchestrator, "AgentInsightsClient", FakeInsights)
    FakeInsights.authorized = True
    progress_events: list[dict[str, object]] = []

    def complete_monitor(**kwargs):
        kwargs["run_started_callback"]("monitor-1", "run-1")
        return monitor, True

    monkeypatch.setattr(
        orchestrator,
        "_complete_monitor",
        complete_monitor,
    )

    final = orchestrator.onboard(
        config,
        run_id=run_id,
        cli=object(),
        progress_callback=progress_events.append,
    )

    run_dir = tmp_path / run_id
    assert final["status"] == "complete"
    assert final["monitor"] == {**asdict(monitor), "insight_count": 1}
    assert final["monitor"]["insight_count"] == 1
    assert final["result_summary"]["insight_count"] == 1
    assert final["result_summary"]["concrete_code_fix_count"] == 0
    assert final["result_summary"]["message"] == (
        "Agent Insights returned 1 insight for the first verified result."
    )
    assert final["agent_insights_portal_url"].endswith(
        "/build/agents/insights-prompt-abc123de/monitor/insights?"
        "tid=22222222-2222-2222-2222-222222222222"
    )
    assert read_json(run_dir / "plan.json")["run_id"] == run_id
    assert read_json(run_dir / "provisioning-receipt.json")["agent"]["version"] == "1"
    assert read_json(run_dir / "traffic-receipt.json")["status"] == "ingested"
    assert progress_events == [read_json(run_dir / "run-started-receipt.json")]
    assert progress_events[0]["status"] == "insights_running"
    assert progress_events[0]["first_run_estimated_minutes"] == {
        "minimum": 10,
        "maximum": 20,
    }
    assert str(progress_events[0]["agent_insights_portal_url"]).endswith(
        "/build/agents/insights-prompt-abc123de/monitor/insights?"
        "tid=22222222-2222-2222-2222-222222222222"
    )
    persisted_final = read_json(run_dir / "final-receipt.json")
    assert persisted_final["status"] == final["status"]
    assert persisted_final["monitor"]["insight_ids"] == ["insight-1"]
    assert persisted_final["result_summary"]["insight_count"] == 1

    with pytest.raises(orchestrator.OnboardingError) as replay:
        orchestrator.onboard(config, run_id=run_id, cli=object())
    assert replay.value.code == "traffic_already_generated"


def test_partial_traffic_is_journaled_and_never_replayed(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    make_deployment,
    azure_context,
    run_id,
) -> None:
    config = make_config()
    resources = make_resources()
    deployment = make_deployment(name="insights-prompt-abc123de", version="1")
    partial = TrafficOutcome(
        scenario="healthy-001",
        expected_fault=False,
        response_id="resp-1",
        session_id=None,
        trace_id=None,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
    )
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_args, **_kwargs: {"status": "ready"})
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    monkeypatch.setattr(
        orchestrator,
        "provision_scratch",
        lambda *_args, **_kwargs: resources,
    )
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(orchestrator, "_wait_for_authorization", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "project_client", lambda *_args: object())
    monkeypatch.setattr(
        orchestrator,
        "create_sample_agent",
        lambda *_args, **_kwargs: deployment,
    )

    def fail_after_one(*_args, outcome_observer, **_kwargs):
        outcome_observer(partial)
        raise orchestrator.OnboardingError(
            "agent_invocation_failed",
            "The bounded invocation failed.",
        )

    monkeypatch.setattr(orchestrator, "generate_sample_traffic", fail_after_one)

    with pytest.raises(orchestrator.OnboardingError) as failed:
        orchestrator.onboard(config, run_id=run_id, cli=object())
    assert failed.value.code == "agent_invocation_failed"
    traffic = read_json(tmp_path / run_id / "traffic-receipt.json")
    assert traffic["status"] == "failed_partial"
    assert len(traffic["outcomes"]) == 1

    with pytest.raises(orchestrator.OnboardingError) as replay:
        orchestrator.onboard(config, run_id=run_id, cli=object())
    assert replay.value.code == "traffic_already_generated"


def test_onboard_existing_reuses_traces_without_invocation(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    make_deployment,
    azure_context,
    azure_ids,
    run_id,
) -> None:
    config = make_config(
        mode="existing",
        location=None,
        agent_type="hosted",
        project_resource_id=azure_ids["project"],
        agent_name="existing-agent",
        model_deployment_name="model",
        application_insights_resource_id=azure_ids["app_insights"],
    )
    resources = make_resources()
    deployment = make_deployment(name="existing-agent", kind="hosted")
    monitor = MonitorOutcome(
        monitor_id="monitor-2",
        run_id="run-2",
        insight_ids=("insight-2",),
        estimated_cost=None,
        enabled=False,
    )
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_args, **_kwargs: {"status": "ready"})
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda *_args, **_kwargs: resources,
    )
    monkeypatch.setattr(
        orchestrator,
        "list_app_insights_connections",
        lambda *_args, **_kwargs: [{"id": "connection"}],
    )
    monkeypatch.setattr(
        orchestrator,
        "_existing_caller_capabilities",
        lambda **_kwargs: (True, True),
    )
    monkeypatch.setattr(orchestrator, "_role_mutations", lambda *_args: [])
    monkeypatch.setattr(
        orchestrator,
        "_apply_existing",
        lambda *_args, **_kwargs: (resources, ()),
    )
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(orchestrator, "project_client", lambda *_args: object())
    monkeypatch.setattr(
        orchestrator,
        "validate_existing_agent",
        lambda *_args, **_kwargs: deployment,
    )
    monkeypatch.setattr(orchestrator, "_credential", lambda _context: object())
    monkeypatch.setattr(
        orchestrator,
        "require_recent_agent_roots",
        lambda **_kwargs: [{"trace_id": "trace-existing", "span_count": 4}],
    )
    monkeypatch.setattr(orchestrator, "AgentInsightsClient", FakeInsights)
    monkeypatch.setattr(
        orchestrator,
        "_complete_monitor",
        lambda **_kwargs: (monitor, False),
    )

    final = orchestrator.onboard(config, run_id=run_id, cli=object())

    assert final["status"] == "complete"
    assert final["monitor"]["enabled"] is False
    traffic = read_json(tmp_path / run_id / "traffic-receipt.json")
    assert traffic["status"] == "ingested"
    assert traffic["outcomes"] == []


def test_onboard_existing_can_create_sample_agent(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    make_deployment,
    azure_context,
    azure_ids,
    run_id,
) -> None:
    config = make_config(
        mode="existing",
        location=None,
        agent_type="prompt",
        project_resource_id=azure_ids["project"],
        agent_name=None,
        model_deployment_name="gpt-5.4",
        application_insights_resource_id=azure_ids["app_insights"],
        create_sample_agent=True,
    )
    resources = make_resources()
    deployment = make_deployment(
        name="insights-prompt-abc123def456",
        version="1",
    )
    outcomes = [
        TrafficOutcome(
            scenario=f"scenario-{index}",
            expected_fault=index > 6,
            response_id=f"resp-{index}",
            session_id=None,
            trace_id=None,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
        )
        for index in range(1, 12)
    ]
    monitor = MonitorOutcome(
        monitor_id="monitor-created",
        run_id="run-created",
        insight_ids=("insight-created",),
        estimated_cost=None,
        enabled=False,
    )
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_args, **_kwargs: {"status": "ready"})
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda *_args, **_kwargs: resources,
    )
    monkeypatch.setattr(
        orchestrator,
        "list_app_insights_connections",
        lambda *_args, **_kwargs: [{"id": "connection"}],
    )
    monkeypatch.setattr(
        orchestrator,
        "_existing_caller_capabilities",
        lambda **_kwargs: (True, True),
    )
    monkeypatch.setattr(orchestrator, "_role_mutations", lambda *_args: [])
    monkeypatch.setattr(
        orchestrator,
        "_apply_existing",
        lambda *_args, **_kwargs: (resources, ()),
    )
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(orchestrator, "_wait_for_authorization", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "project_client", lambda *_args: object())
    monkeypatch.setattr(
        orchestrator,
        "create_sample_agent",
        lambda *_args, **_kwargs: deployment,
    )
    monkeypatch.setattr(
        orchestrator,
        "validate_existing_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Existing Agent validation must not run")
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "generate_sample_traffic",
        lambda *_args, **_kwargs: outcomes,
    )
    monkeypatch.setattr(orchestrator, "_credential", lambda _context: object())
    monkeypatch.setattr(
        orchestrator,
        "wait_for_ingestion",
        lambda **_kwargs: [{"trace_id": "trace-created"}],
    )
    monkeypatch.setattr(orchestrator, "AgentInsightsClient", FakeInsights)
    monkeypatch.setattr(
        orchestrator,
        "_complete_monitor",
        lambda **_kwargs: (monitor, True),
    )

    final = orchestrator.onboard(config, run_id=run_id, cli=object())

    run_dir = tmp_path / run_id
    provisioning = read_json(run_dir / "provisioning-receipt.json")
    assert provisioning["agent_created"] is True
    assert provisioning["agent"]["name"] == "insights-prompt-abc123def456"
    assert len(read_json(run_dir / "traffic-receipt.json")["outcomes"]) == 11
    assert final["feedback_url"].startswith(
        "https://msdata.visualstudio.com/Vienna/_workitems/create/Bug"
    )


def test_cleanup_scratch_uses_receipt_owned_group(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    azure_context,
    run_id,
) -> None:
    config = make_config()
    resources = make_resources()
    plan = orchestrator.build_plan(
        config,
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )
    run_dir = tmp_path / run_id
    orchestrator.write_json_atomic(run_dir / "plan.json", plan.as_dict())
    orchestrator.write_json_atomic(
        run_dir / "provisioning-receipt.json",
        {
            "status": "complete",
            "project": asdict(resources),
            "agent": {
                "name": "agent",
                "version": "1",
                "kind": "prompt",
                "artifact_sha256": None,
            },
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator,
        "cleanup_scratch",
        lambda _cli, **kwargs: calls.append(kwargs),
    )

    result = orchestrator.cleanup(run_dir, cli=object())

    assert result["status"] == "complete"
    assert calls == [
        {
            "resource_group_id": resources.resource_group_id,
            "run_id": run_id,
            "owner_object_id": azure_context.user_object_id,
        }
    ]


def test_cleanup_existing_removes_only_receipt_owned_sample_agent(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    make_deployment,
    azure_context,
    azure_ids,
    run_id,
) -> None:
    config = make_config(
        mode="existing",
        location=None,
        agent_type="prompt",
        project_resource_id=azure_ids["project"],
        agent_name=None,
        model_deployment_name="gpt-5.4",
        application_insights_resource_id=azure_ids["app_insights"],
        create_sample_agent=True,
    )
    resources = make_resources()
    deployment = make_deployment(
        name="insights-prompt-abc123def456",
        version="1",
    )
    plan = orchestrator.OnboardingPlan.create(
        run_id=run_id,
        config=config,
        context=azure_context,
        mutations=[
            orchestrator.Mutation(
                "create_sample_agent_version",
                deployment.name,
                {"agent_type": "prompt", "immutable": True},
            )
        ],
        expected={"first_result": "nonempty"},
    )
    run_dir = tmp_path / run_id
    orchestrator.write_json_atomic(run_dir / "plan.json", plan.as_dict())
    orchestrator.write_json_atomic(
        run_dir / "provisioning-receipt.json",
        {
            "status": "complete",
            "project": asdict(resources),
            "agent": asdict(deployment),
            "agent_created": True,
            "created_connection_ids": [],
            "created_role_assignments": [],
        },
    )
    orchestrator.write_json_atomic(
        run_dir / "insights-receipt.json",
        {
            "status": "complete",
            "monitor_id": "monitor-created",
            "monitor_created": True,
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: azure_context,
    )
    deleted_monitors: list[str] = []

    class CleanupInsights:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def get_monitor(self, monitor_id):
            return {
                "id": monitor_id,
                "agent_name": deployment.name,
            }

        def delete_monitor(self, monitor_id):
            deleted_monitors.append(monitor_id)

    monkeypatch.setattr(orchestrator, "AgentInsightsClient", CleanupInsights)
    monkeypatch.setattr(orchestrator, "_credential", lambda _context: object())
    monkeypatch.setattr(orchestrator, "project_client", lambda *_args: object())
    deleted_agents: list[tuple[object, object, str]] = []
    monkeypatch.setattr(
        orchestrator,
        "delete_owned_agent",
        lambda project, *, deployment, run_id: deleted_agents.append(
            (project, deployment, run_id)
        ),
    )

    result = orchestrator.cleanup(run_dir, cli=object())

    assert result["status"] == "complete"
    assert deleted_monitors == ["monitor-created"]
    assert len(deleted_agents) == 1
    assert deleted_agents[0][1] == deployment
    assert deleted_agents[0][2] == run_id


def test_cleanup_rejects_changed_user_context(
    monkeypatch,
    tmp_path,
    make_config,
    make_resources,
    azure_context,
    run_id,
) -> None:
    config = make_config()
    resources = make_resources()
    plan = orchestrator.build_plan(
        config,
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )
    run_dir = tmp_path / run_id
    orchestrator.write_json_atomic(run_dir / "plan.json", plan.as_dict())
    orchestrator.write_json_atomic(
        run_dir / "provisioning-receipt.json",
        {
            "status": "complete",
            "project": asdict(resources),
            "agent": {
                "name": "agent",
                "version": "1",
                "kind": "prompt",
                "artifact_sha256": None,
            },
        },
    )
    changed_context = type(azure_context)(
        **{
            **asdict(azure_context),
            "user_object_id": "99999999-9999-9999-9999-999999999999",
        }
    )
    monkeypatch.setattr(
        orchestrator,
        "select_context",
        lambda _cli, _subscription_id: changed_context,
    )

    with pytest.raises(orchestrator.OnboardingError) as excinfo:
        orchestrator.cleanup(run_dir, cli=object())
    assert excinfo.value.code == "plan_context_changed"


def test_existing_monitor_reuses_successful_result(
    tmp_path,
    make_deployment,
) -> None:
    class ExistingResultClient:
        def get_or_create_monitor(self, **_kwargs):
            return {
                "id": "monitor-existing",
                "enabled": True,
                "run_interval_hours": 24,
                "next_scheduled_run_at": "2026-01-02T00:00:00+00:00",
                "estimated_cost": {"amount": 1.5, "currency": "USD"},
            }, False

        def list_insights(self, _monitor_id):
            return [{"id": "insight-existing"}]

        def list_runs(self, _monitor_id):
            return [
                {"id": "run-existing", "status": "succeeded"},
                {"id": "run-failed", "status": "failed"},
            ]

        def create_run(self, *_args, **_kwargs):
            raise AssertionError("Existing successful result must not create a run")

    outcome, created = orchestrator._complete_monitor(
        client=ExistingResultClient(),
        run_dir=tmp_path,
        deployment=make_deployment(),
        model_deployment_name="model",
        enable_monitor=False,
        lookback_hours=720,
        allow_existing_result=True,
        timeout_seconds=10,
    )

    assert created is False
    assert outcome.run_id == "run-existing"
    assert outcome.insight_ids == ("insight-existing",)
    assert outcome.run_interval_hours == 24
    assert outcome.next_scheduled_run_at == "2026-01-02T00:00:00+00:00"
    assert read_json(tmp_path / "insights-receipt.json")[
        "reused_existing_run"
    ] is True


def test_new_insights_run_reports_portal_handoff_before_waiting(
    tmp_path,
    make_deployment,
) -> None:
    started: list[tuple[str, str]] = []

    class NewRunClient:
        def get_or_create_monitor(self, **_kwargs):
            return {
                "id": "monitor-new",
                "enabled": False,
                "run_interval_hours": 24,
            }, True

        def create_run(self, _monitor_id, **_kwargs):
            return {"id": "run-new"}

        def wait_run(self, **_kwargs):
            assert started == [("monitor-new", "run-new")]

        def list_insights(self, _monitor_id):
            return [{"id": "insight-new"}]

        def get_monitor(self, _monitor_id):
            return {
                "id": "monitor-new",
                "enabled": False,
                "run_interval_hours": 24,
            }

    outcome, created = orchestrator._complete_monitor(
        client=NewRunClient(),
        run_dir=tmp_path,
        deployment=make_deployment(),
        model_deployment_name="model",
        enable_monitor=False,
        lookback_hours=168,
        allow_existing_result=False,
        timeout_seconds=10,
        run_started_callback=lambda monitor_id, run_id: started.append(
            (monitor_id, run_id)
        ),
    )

    assert created is True
    assert outcome.run_id == "run-new"
    assert read_json(tmp_path / "insights-state.json")["status"] == "started"


@pytest.mark.parametrize(("fix_kind", "expected_error"), (("code_change", None), ("prose", True)))
def test_hosted_sample_requires_a_validated_concrete_code_fix(
    tmp_path,
    make_deployment,
    fix_kind,
    expected_error,
) -> None:
    class HostedFixClient:
        def get_or_create_monitor(self, **_kwargs):
            return {"id": "monitor-hosted", "enabled": False}, True

        def create_run(self, _monitor_id, **_kwargs):
            return {"id": "run-hosted"}

        def wait_run(self, **_kwargs):
            return {"status": "succeeded"}

        def list_insights(self, _monitor_id, *, include_details=False):
            assert include_details is True
            proposed_fix = (
                {
                    "kind": "code_change",
                    "changes": [{"path": "main.py", "diff": "diff"}],
                }
                if fix_kind == "code_change"
                else {"kind": "prose", "text": "Investigate the timeout."}
            )
            return [
                {
                    "id": "insight-hosted",
                    "details": {
                        "recommended_actions": {
                            "proposed_fix": proposed_fix,
                        }
                    },
                }
            ]

        def get_monitor(self, _monitor_id):
            return {"id": "monitor-hosted", "enabled": False}

    deployment = make_deployment(
        name="insights-hosted-demo",
        kind="hosted",
        artifact_sha256="a" * 64,
    )
    if expected_error:
        with pytest.raises(orchestrator.OnboardingError) as missing_fix:
            orchestrator._complete_monitor(
                client=HostedFixClient(),
                run_dir=tmp_path,
                deployment=deployment,
                model_deployment_name="gpt-5.6-terra",
                enable_monitor=False,
                lookback_hours=168,
                allow_existing_result=False,
                timeout_seconds=10,
            )
        assert missing_fix.value.code == "missing_concrete_code_fix"
        return

    outcome, _ = orchestrator._complete_monitor(
        client=HostedFixClient(),
        run_dir=tmp_path,
        deployment=deployment,
        model_deployment_name="gpt-5.6-terra",
        enable_monitor=False,
        lookback_hours=168,
        allow_existing_result=False,
        timeout_seconds=10,
    )
    assert outcome.concrete_code_fix_count == 1
