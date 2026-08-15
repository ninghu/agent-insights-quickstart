from __future__ import annotations

from datetime import UTC, datetime

import pytest
from insights_onboarding import models, orchestrator
from insights_onboarding.errors import OnboardingError
from insights_onboarding.ingestion import _correlate, _query
from insights_onboarding.models import Mutation, TrafficOutcome


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz or UTC)


def test_recent_trace_query_filters_version_and_bounds_roots() -> None:
    query = _query(
        "existing-agent",
        agent_version="7",
        root_limit=3,
    )

    assert "agent_name == 'existing-agent'" in query
    assert "agent_version == '7'" in query
    assert "| take 3" in query


def test_plan_hash_contract_is_deterministic_when_clock_is_frozen(
    monkeypatch, make_config, azure_context, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    plan_one = models.OnboardingPlan.create(
        run_id=run_id,
        config=make_config(),
        context=azure_context,
        mutations=[Mutation("noop", "resource")],
        expected={"traffic": {"total": 11}},
    )
    plan_two = models.OnboardingPlan.create(
        run_id=run_id,
        config=make_config(),
        context=azure_context,
        mutations=[Mutation("noop", "resource")],
        expected={"traffic": {"total": 11}},
    )

    assert plan_one.created_at == "2026-01-02T03:04:05+00:00"
    assert plan_one.plan_hash == plan_two.plan_hash
    assert plan_one.mutations == (
        {"kind": "noop", "target": "resource", "properties": {}},
    )


def test_build_plan_for_scratch_contains_expected_mutations(
    monkeypatch, make_config, azure_context, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    plan = orchestrator.build_plan(
        make_config(mode="scratch", location="westus3", agent_type="prompt"),
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )

    assert [mutation["kind"] for mutation in plan.mutations] == [
        "create_resource_group",
        "deploy_scratch_environment",
        "create_sample_agent_version",
        "generate_bounded_traffic",
        "create_or_reuse_monitor",
        "run_agent_insights",
        "enable_monitor",
    ]
    assert plan.expected["traffic"] == {"healthy": 6, "fault": 5, "total": 11}
    assert plan.expected["monitor_enabled"] is True


def test_build_plan_for_existing_covers_role_connection_and_monitor_paths(
    monkeypatch, make_config, make_resources, azure_context, azure_ids, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda _cli, config: make_resources(
            project_principal_id="44444444-4444-4444-4444-444444444444"
        ),
    )
    monkeypatch.setattr(orchestrator, "list_app_insights_connections", lambda *_args: [])
    monkeypatch.setattr(
        orchestrator,
        "plan_existing_connections",
        lambda *_args, **_kwargs: {
            "project_connection_name": "agent-insights-test",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_role_mutations",
        lambda _cli, _assignments: [Mutation("create_role_assignment", azure_ids["project"])],
    )
    monkeypatch.setattr(
        orchestrator,
        "_existing_caller_capabilities",
        lambda **_kwargs: (False, False),
    )

    plan = orchestrator.build_plan(
        make_config(
            mode="existing",
            location=None,
            project_resource_id=azure_ids["project"],
            agent_name="existing-agent",
            model_deployment_name="gpt-4-1-mini",
            agent_type="hosted",
            enable_existing_monitor=True,
        ),
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )

    assert [mutation["kind"] for mutation in plan.mutations] == [
        "create_app_insights_connections",
        "create_role_assignment",
        "create_or_reuse_monitor",
        "create_or_reuse_agent_insights_result",
        "enable_monitor",
    ]
    assert plan.expected["traffic"] == {"generated": 0}
    assert plan.expected["monitor_enabled"] is True


def test_build_plan_creates_sample_agent_in_existing_project(
    monkeypatch, make_config, make_resources, azure_context, azure_ids, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda _cli, config: make_resources(),
    )
    monkeypatch.setattr(
        orchestrator,
        "list_app_insights_connections",
        lambda *_args: [{"id": "connection"}],
    )
    monkeypatch.setattr(
        orchestrator,
        "_role_mutations",
        lambda _cli, _assignments: [
            Mutation("create_role_assignment", azure_ids["project"])
        ],
    )
    monkeypatch.setattr(
        orchestrator,
        "_existing_caller_capabilities",
        lambda **_kwargs: (False, False),
    )

    plan = orchestrator.build_plan(
        make_config(
            mode="existing",
            location=None,
            project_resource_id=azure_ids["project"],
            agent_name=None,
            model_deployment_name="gpt-5.4",
            agent_type="prompt",
            create_sample_agent=True,
        ),
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )

    assert [mutation["kind"] for mutation in plan.mutations] == [
        "create_role_assignment",
        "create_sample_agent_version",
        "generate_bounded_traffic",
        "create_or_reuse_monitor",
        "run_agent_insights",
    ]
    assert plan.mutations[1]["target"] == "insights-prompt-abc123def456"
    assert plan.expected["traffic"] == {"healthy": 6, "fault": 5, "total": 11}
    assert plan.expected["monitor_enabled"] is False


def test_build_plan_scheduled_adds_identity_mutation_when_project_has_no_principal(
    monkeypatch, make_config, make_resources, azure_context, azure_ids, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda _cli, config: make_resources(project_principal_id=""),
    )
    monkeypatch.setattr(orchestrator, "list_app_insights_connections", lambda *_args: [])
    monkeypatch.setattr(
        orchestrator,
        "plan_existing_connections",
        lambda *_args, **_kwargs: {
            "project_connection_name": "agent-insights-test",
        },
    )

    plan = orchestrator.build_plan(
        make_config(
            mode="existing",
            location=None,
            project_resource_id=azure_ids["project"],
            agent_name="existing-agent",
            model_deployment_name="gpt-4-1-mini",
            agent_type="prompt",
            enable_existing_monitor=True,
        ),
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )

    kinds = [mutation["kind"] for mutation in plan.mutations]
    assert kinds[:2] == [
        "enable_project_system_identity",
        "create_app_insights_connections",
    ]
    assert kinds.count("ensure_role_assignment_after_identity") == 5
    assert kinds[-3:] == [
        "create_or_reuse_monitor",
        "create_or_reuse_agent_insights_result",
        "enable_monitor",
    ]


def test_build_plan_one_off_skips_project_identity_mutations(
    monkeypatch, make_config, make_resources, azure_context, azure_ids, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda _cli, config: make_resources(project_principal_id=""),
    )
    monkeypatch.setattr(orchestrator, "list_app_insights_connections", lambda *_args: [])
    monkeypatch.setattr(
        orchestrator,
        "plan_existing_connections",
        lambda *_args, **_kwargs: {
            "project_connection_name": "agent-insights-test",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_existing_caller_capabilities",
        lambda **_kwargs: (False, False),
    )
    planned_assignments = []

    def capture_assignments(_cli, assignments):
        planned_assignments.extend(assignments)
        return []

    monkeypatch.setattr(orchestrator, "_role_mutations", capture_assignments)

    plan = orchestrator.build_plan(
        make_config(
            mode="existing",
            location=None,
            project_resource_id=azure_ids["project"],
            agent_name="existing-agent",
            model_deployment_name="gpt-4-1-mini",
            agent_type="prompt",
        ),
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )

    kinds = [mutation["kind"] for mutation in plan.mutations]
    assert "enable_project_system_identity" not in kinds
    assert "ensure_role_assignment_after_identity" not in kinds
    assert all(item.principal_type == "User" for item in planned_assignments)


@pytest.mark.parametrize(
    ("enable_schedule", "resolved_principal_id", "expected_identity_updates"),
    (
        (False, "", 0),
        (True, "44444444-4444-4444-4444-444444444444", 1),
    ),
)
def test_apply_existing_enables_project_identity_only_for_schedule(
    monkeypatch,
    make_config,
    make_resources,
    azure_context,
    run_id: str,
    enable_schedule: bool,
    resolved_principal_id: str,
    expected_identity_updates: int,
) -> None:
    resources = iter(
        (
            make_resources(project_principal_id=""),
            make_resources(project_principal_id=resolved_principal_id),
        )
    )
    monkeypatch.setattr(
        orchestrator,
        "resolve_existing",
        lambda *_args, **_kwargs: next(resources),
    )
    identity_updates: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "ensure_project_identity",
        lambda _cli, *, project_resource_id: identity_updates.append(
            project_resource_id
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "get_project",
        lambda *_args, **_kwargs: {"location": "westus3"},
    )
    monkeypatch.setattr(
        orchestrator,
        "ensure_existing_connections",
        lambda *_args, **_kwargs: (),
    )

    result, connection_ids = orchestrator._apply_existing(
        object(),
        config=make_config(
            mode="existing",
            location=None,
            project_resource_id=make_resources().project_resource_id,
            agent_name="existing-agent",
            model_deployment_name="gpt-4-1-mini",
            agent_type="prompt",
            enable_existing_monitor=enable_schedule,
        ),
        context=azure_context,
        run_id=run_id,
    )

    assert result.project_principal_id == resolved_principal_id
    assert connection_ids == ()
    assert len(identity_updates) == expected_identity_updates


def test_verify_stored_plan_detects_mutation_tampering(
    monkeypatch, make_config, azure_context, run_id: str
) -> None:
    monkeypatch.setattr(models, "datetime", FrozenDateTime)
    payload = models.OnboardingPlan.create(
        run_id=run_id,
        config=make_config(),
        context=azure_context,
        mutations=[Mutation("noop", "resource")],
        expected={"traffic": {"total": 11}},
    ).as_dict()
    payload["expected"]["traffic"]["total"] = 99

    with pytest.raises(OnboardingError) as excinfo:
        orchestrator._verify_stored_plan(payload)

    assert excinfo.value.code == "plan_hash_mismatch"


def test_ingestion_correlation_matches_exact_response_and_session_ids(
    make_deployment,
) -> None:
    deployment = make_deployment(version="9.9.9")
    outcomes = [
        TrafficOutcome(
            scenario="healthy-001",
            expected_fault=False,
            response_id="resp-1",
            session_id=None,
            trace_id=None,
            started_at="2026-01-02T03:04:05+00:00",
            completed_at="2026-01-02T03:04:06+00:00",
        ),
        TrafficOutcome(
            scenario="healthy-002",
            expected_fault=False,
            response_id=None,
            session_id="session-2",
            trace_id=None,
            started_at="2026-01-02T03:04:05+00:00",
            completed_at="2026-01-02T03:04:06+00:00",
        ),
    ]
    rows = [
        {
            "trace_id": "trace-1",
            "versions": '["9.9.9"]',
            "response_ids": '["resp-1"]',
            "hosted_response_ids": "[]",
            "hosted_session_ids": "[]",
            "tool_names": '["lookup_order"]',
            "span_count": 4,
        },
        {
            "trace_id": "trace-2",
            "versions": ["9.9.9"],
            "response_ids": "[]",
            "hosted_response_ids": "[]",
            "hosted_session_ids": '["session-2"]',
            "tool_names": ["lookup_order"],
            "span_count": 5,
        },
    ]

    assert _correlate(rows, outcomes, deployment, require_tool=True) == [
        {
            "scenario": "healthy-001",
            "trace_id": "trace-1",
            "response_id": "resp-1",
            "session_id": None,
            "span_count": 4,
        },
        {
            "scenario": "healthy-002",
            "trace_id": "trace-2",
            "response_id": None,
            "session_id": "session-2",
            "span_count": 5,
        },
    ]


def test_ingestion_correlation_enforces_versions_tools_duplicates_and_partials(
    make_deployment,
) -> None:
    deployment = make_deployment(version="1.2.3")
    outcome = TrafficOutcome(
        scenario="healthy-001",
        expected_fault=False,
        response_id="resp-1",
        session_id=None,
        trace_id=None,
        started_at="2026-01-02T03:04:05+00:00",
        completed_at="2026-01-02T03:04:06+00:00",
    )
    bad_version = [
        {
            "trace_id": "trace-1",
            "versions": ["0.0.1"],
            "response_ids": ["resp-1"],
            "hosted_response_ids": [],
            "hosted_session_ids": [],
            "tool_names": ["lookup_order"],
            "span_count": 1,
        }
    ]
    with pytest.raises(OnboardingError) as version_error:
        _correlate(bad_version, [outcome], deployment, require_tool=True)
    assert version_error.value.code == "ingestion_version_mismatch"

    missing_tool = [
        {
            "trace_id": "trace-1",
            "versions": ["1.2.3"],
            "response_ids": ["resp-1"],
            "hosted_response_ids": [],
            "hosted_session_ids": [],
            "tool_names": [],
            "span_count": 1,
        }
    ]
    assert _correlate(missing_tool, [outcome], deployment, require_tool=True) is None

    duplicate_rows = [
        {
            "trace_id": "trace-1",
            "versions": ["1.2.3"],
            "response_ids": ["resp-1", "resp-2"],
            "hosted_response_ids": [],
            "hosted_session_ids": [],
            "tool_names": ["lookup_order"],
            "span_count": 2,
        }
    ]
    duplicate_outcomes = [
        outcome,
        TrafficOutcome(
            scenario="healthy-002",
            expected_fault=False,
            response_id="resp-2",
            session_id=None,
            trace_id=None,
            started_at="2026-01-02T03:04:05+00:00",
            completed_at="2026-01-02T03:04:06+00:00",
        ),
    ]
    with pytest.raises(OnboardingError) as duplicate:
        _correlate(duplicate_rows, duplicate_outcomes, deployment, require_tool=True)
    assert duplicate.value.code == "duplicate_ingestion_correlation"

    assert _correlate([], [outcome], deployment, require_tool=False) is None
