from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from insights_onboarding import live_matrix, models, orchestrator
from insights_onboarding.errors import OnboardingError
from insights_onboarding.live_matrix import (
    SUPPORTED_CASES,
    FixtureProject,
    LiveMatrixOptions,
    case_config,
    cleanup_matrix_groups,
    run_live_matrix,
    select_cases,
    supported_case_names,
    validate_case_result,
)


def _options(tmp_path: Path) -> LiveMatrixOptions:
    return LiveMatrixOptions(
        subscription_id="11111111-1111-1111-1111-111111111111",
        location="westus3",
        model_name="gpt-5.4",
        model_version="2026-03-05",
        model_format="OpenAI",
        model_sku="GlobalStandard",
        model_capacity=30,
        cases=SUPPORTED_CASES,
        output_dir=tmp_path,
        ingestion_timeout_seconds=900,
        insights_timeout_seconds=2400,
    )


def test_supported_path_matrix_is_complete_and_stably_ordered() -> None:
    assert supported_case_names() == (
        "scratch-prompt-scheduled",
        "scratch-hosted-scheduled",
        "scratch-prompt-protected-scheduled",
        "existing-create-prompt-oneoff",
        "existing-create-prompt-scheduled",
        "existing-create-hosted-oneoff",
        "existing-create-hosted-scheduled",
        "existing-select-prompt-oneoff",
        "existing-select-prompt-scheduled",
        "existing-select-hosted-oneoff",
        "existing-select-hosted-scheduled",
        "existing-create-prompt-protected-scheduled",
    )
    assert select_cases("existing-select-hosted-scheduled,scratch-prompt-scheduled") == (
        SUPPORTED_CASES[0],
        SUPPORTED_CASES[10],
    )


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=lambda case: case.name)
def test_every_supported_path_has_the_expected_frozen_plan(
    case,
    monkeypatch,
    tmp_path,
    make_resources,
    make_deployment,
    azure_context,
    run_id,
) -> None:
    options = _options(tmp_path)
    resources = make_resources()
    fixture = FixtureProject(
        run_id="fixture12345",
        config=models.OnboardingConfig(
            mode="scratch",
            subscription_id=options.subscription_id,
            location=options.location,
            agent_type="hosted",
        ),
        context=azure_context,
        resources=resources,
    )
    selected_agent = (
        make_deployment(name=f"selected-{case.agent_type}", kind=case.agent_type)
        if case.agent_selection == "select"
        else None
    )
    config = case_config(
        case,
        options,
        fixture=fixture if case.mode == "existing" else None,
        selected_agent=selected_agent,
    )
    if case.mode == "existing":
        monkeypatch.setattr(
            orchestrator,
            "resolve_existing",
            lambda *_args, **_kwargs: resources,
        )
        monkeypatch.setattr(
            orchestrator,
            "list_app_insights_connections",
            lambda *_args: (
                []
                if case.connection_state == "missing"
                else [{"id": "connection"}]
            ),
        )
        monkeypatch.setattr(
            orchestrator,
            "plan_existing_connections",
            lambda *_args, **_kwargs: {
                "project_connection_name": "matrix-appinsights",
            },
        )
        monkeypatch.setattr(
            orchestrator,
            "_existing_caller_capabilities",
            lambda **_kwargs: (True, True),
        )
        monkeypatch.setattr(orchestrator, "_role_mutations", lambda *_args: [])

    plan = orchestrator.build_plan(
        config,
        context=azure_context,
        run_id=run_id,
        cli=object(),
    )
    kinds = [mutation["kind"] for mutation in plan.mutations]

    assert plan.expected["first_run_trigger"] == (
        "scheduled" if case.scheduled else "manual"
    )
    assert ("enable_monitor" in kinds) is case.scheduled
    assert ("wait_for_scheduled_agent_insights_result" in kinds) is case.scheduled
    assert ("create_sample_agent_version" in kinds) is (
        case.agent_selection in {"automatic", "create"}
    )
    assert ("generate_bounded_traffic" in kinds) is (
        case.agent_selection in {"automatic", "create"}
    )
    assert config.protected_trace_content is case.protected_trace_content
    if not case.scheduled:
        expected_manual = (
            "run_agent_insights"
            if case.agent_selection in {"automatic", "create"}
            else "create_or_reuse_agent_insights_result"
        )
        assert expected_manual in kinds


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=lambda case: case.name)
def test_every_supported_path_has_explicit_result_assertions(case) -> None:
    summary = {
        "insight_count": 1,
        "schedule_enabled": case.scheduled,
        "first_run_trigger": "scheduled" if case.scheduled else "manual",
        "concrete_prompt_fix_count": (
            1
            if case.agent_type == "prompt"
            and case.agent_selection in {"automatic", "create"}
            else 0
        ),
        "concrete_code_fix_count": (
            1
            if case.agent_type == "hosted"
            and case.agent_selection in {"automatic", "create"}
            else 0
        ),
    }
    result = {
        "status": "complete",
        "agent": {"kind": case.agent_type},
        "result_summary": summary,
        "agent_insights_portal_url": (
            "https://ai.azure.com/nextgen/r/context/build/agents/demo/"
            "monitor/insights?tid=tenant"
        ),
    }

    assertion = validate_case_result(case, result)

    assert assertion["status"] == "passed"
    assert assertion["first_run_trigger"] == summary["first_run_trigger"]


def test_matrix_assertions_fail_on_wrong_trigger_and_missing_fix() -> None:
    case = next(
        item
        for item in SUPPORTED_CASES
        if item.name == "existing-create-prompt-scheduled"
    )
    result = {
        "status": "complete",
        "agent": {"kind": "prompt"},
        "result_summary": {
            "insight_count": 1,
            "schedule_enabled": True,
            "first_run_trigger": "manual",
            "concrete_prompt_fix_count": 0,
            "concrete_code_fix_count": 0,
        },
        "agent_insights_portal_url": (
            "https://ai.azure.com/nextgen/r/context/build/agents/demo/"
            "monitor/insights?tid=tenant"
        ),
    }

    with pytest.raises(OnboardingError) as wrong_trigger:
        validate_case_result(case, result)
    assert wrong_trigger.value.code == "live_matrix_trigger_mismatch"

    result["result_summary"]["first_run_trigger"] = "scheduled"
    with pytest.raises(OnboardingError) as missing_fix:
        validate_case_result(case, result)
    assert missing_fix.value.code == "live_matrix_concrete_fix_missing"


def test_live_workflow_is_guarded_and_uses_an_interactive_self_hosted_runner(
    repo_root: Path,
) -> None:
    workflow = (
        repo_root / ".github" / "workflows" / "live-matrix.yml"
    ).read_text(encoding="utf-8")

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "AGENT_INSIGHTS_LIVE_ENABLED" in workflow
    assert "self-hosted" in workflow
    assert "agent-insights-live" in workflow
    assert "user.type" in workflow
    assert "agent_insights_live_matrix.py" in workflow
    assert "actions/upload-artifact@v6" in workflow
    assert "cleanup:" in workflow
    assert "always()" in workflow
    assert "--confirm-live 2>&1" in workflow
    assert "azure/login" not in workflow


def test_runner_executes_all_selected_scratch_cases_and_writes_summary(
    monkeypatch,
    tmp_path,
) -> None:
    options = replace(_options(tmp_path), cases=SUPPORTED_CASES[:2])
    executed: list[str] = []

    def run_case(case, *_args, **_kwargs):
        executed.append(case.name)
        return {
            "case": case.name,
            "status": "passed",
            "duration_seconds": 1,
            "cleanup_status": "complete",
            "assertion": {},
            "error_code": None,
        }

    monkeypatch.setattr(
        "insights_onboarding.live_matrix._run_case",
        run_case,
    )

    summary = run_live_matrix(options, cli=object())

    assert executed == list(supported_case_names()[:2])
    assert summary["status"] == "passed"
    assert summary["case_count"] == 2
    assert (tmp_path / "summary.json").exists()


def test_runner_owns_and_cleans_existing_project_fixture(
    monkeypatch,
    tmp_path,
    make_resources,
    azure_context,
) -> None:
    selected_case = next(
        case
        for case in SUPPORTED_CASES
        if case.name == "existing-create-prompt-oneoff"
    )
    options = replace(_options(tmp_path), cases=(selected_case,))
    fixture = FixtureProject(
        run_id="fixture12345",
        config=models.OnboardingConfig(
            mode="scratch",
            subscription_id=options.subscription_id,
            location=options.location,
            agent_type="hosted",
        ),
        context=azure_context,
        resources=make_resources(),
    )
    lifecycle: list[str] = []
    monkeypatch.setattr(
        "insights_onboarding.live_matrix._create_fixture",
        lambda *_args: fixture,
    )
    monkeypatch.setattr(
        "insights_onboarding.live_matrix._delete_fixture_connections",
        lambda *_args: lifecycle.append("connections_deleted"),
    )
    monkeypatch.setattr(
        "insights_onboarding.live_matrix._run_case",
        lambda case, *_args, **_kwargs: {
            "case": case.name,
            "status": "passed",
            "duration_seconds": 1,
            "cleanup_status": "complete",
            "assertion": {},
            "error_code": None,
        },
    )
    monkeypatch.setattr(
        "insights_onboarding.live_matrix._cleanup_fixture",
        lambda *_args: lifecycle.append("fixture_cleaned"),
    )

    summary = run_live_matrix(options, cli=object())

    assert summary["status"] == "passed"
    assert lifecycle == ["connections_deleted", "fixture_cleaned"]
    assert summary["fixture_cleanup_status"] == "complete"


def test_unexpected_case_failure_still_runs_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    options = replace(_options(tmp_path), cases=(SUPPORTED_CASES[0],))
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "onboard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("transport failed")
        ),
    )
    monkeypatch.setattr(
        live_matrix,
        "_cleanup_case",
        lambda run_id, **_kwargs: cleanup_calls.append(run_id) or "complete",
    )
    monkeypatch.setattr(
        live_matrix,
        "_verify_scratch_group_absent",
        lambda *_args: None,
    )

    result = live_matrix._run_case(
        SUPPORTED_CASES[0],
        options,
        cli=object(),
        fixture=None,
        selected_agent=None,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "live_matrix_unexpected_failure"
    assert len(cleanup_calls) == 1


def test_fixture_cleanup_runs_after_selected_agent_cleanup_failure(
    monkeypatch,
    tmp_path,
    make_resources,
    make_deployment,
    azure_context,
) -> None:
    selected_case = next(
        case
        for case in SUPPORTED_CASES
        if case.name == "existing-select-prompt-oneoff"
    )
    options = replace(_options(tmp_path), cases=(selected_case,))
    fixture = FixtureProject(
        run_id="fixture12345",
        config=models.OnboardingConfig(
            mode="scratch",
            subscription_id=options.subscription_id,
            location=options.location,
            agent_type="hosted",
        ),
        context=azure_context,
        resources=make_resources(),
    )
    deployment = make_deployment(name="selected-prompt", kind="prompt")
    lifecycle: list[str] = []
    monkeypatch.setattr(
        live_matrix,
        "_create_fixture",
        lambda *_args: fixture,
    )
    monkeypatch.setattr(
        live_matrix,
        "_create_selected_agent_fixture",
        lambda *_args: ("selected1234", deployment),
    )
    monkeypatch.setattr(
        live_matrix,
        "_ensure_fixture_connections",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        live_matrix,
        "_run_case",
        lambda case, *_args, **_kwargs: {
            "case": case.name,
            "status": "passed",
            "duration_seconds": 1,
            "cleanup_status": "complete",
            "assertion": {},
            "error_code": None,
        },
    )
    monkeypatch.setattr(
        live_matrix,
        "_delete_fixture_agent_monitors",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("SDK failure")),
    )
    monkeypatch.setattr(
        live_matrix,
        "_cleanup_fixture",
        lambda *_args: lifecycle.append("fixture_cleaned"),
    )

    summary = run_live_matrix(options, cli=object())

    assert summary["status"] == "passed"
    assert lifecycle == ["fixture_cleaned"]
    assert summary["fixture_cleanup_status"] == "complete"


def test_cleanup_only_deletes_owned_matrix_groups(
    monkeypatch,
    azure_context,
) -> None:
    groups = [
        {
            "id": "/subscriptions/sub/resourceGroups/rg-insights-live-fixture-one",
            "name": "rg-insights-live-fixture-one",
            "tags": {
                "created-by": "agent-insights-quickstart",
                "run-id": "run-one",
                "owner-object-id": azure_context.user_object_id,
            },
        },
        {
            "id": "/subscriptions/sub/resourceGroups/customer-group",
            "name": "customer-group",
            "tags": {
                "created-by": "agent-insights-quickstart",
                "run-id": "customer",
                "owner-object-id": azure_context.user_object_id,
            },
        },
    ]

    class GroupCli:
        def json(self, arguments, **_kwargs):
            assert arguments[:2] == ["group", "list"]
            return groups

    deleted: list[str] = []
    monkeypatch.setattr(
        live_matrix,
        "select_context",
        lambda *_args: azure_context,
    )
    monkeypatch.setattr(
        live_matrix,
        "cleanup_scratch",
        lambda _cli, **kwargs: deleted.append(kwargs["resource_group_id"]),
    )

    result = cleanup_matrix_groups(GroupCli(), azure_context.subscription_id)

    assert result["deleted_resource_groups"] == [
        "rg-insights-live-fixture-one"
    ]
    assert deleted == [groups[0]["id"]]


def test_cleanup_only_attempts_every_owned_group_before_failing(
    monkeypatch,
    azure_context,
) -> None:
    groups = [
        {
            "id": f"/subscriptions/sub/resourceGroups/rg-insights-live-matrix-{index}",
            "name": f"rg-insights-live-matrix-{index}",
            "tags": {
                "created-by": "agent-insights-quickstart",
                "run-id": f"run-{index}",
                "owner-object-id": azure_context.user_object_id,
            },
        }
        for index in (1, 2)
    ]

    class GroupCli:
        def json(self, _arguments, **_kwargs):
            return groups

    attempted: list[str] = []

    def cleanup(_cli, **kwargs):
        attempted.append(kwargs["resource_group_id"])
        if len(attempted) == 1:
            raise RuntimeError("first delete failed")

    monkeypatch.setattr(
        live_matrix,
        "select_context",
        lambda *_args: azure_context,
    )
    monkeypatch.setattr(live_matrix, "cleanup_scratch", cleanup)

    with pytest.raises(OnboardingError) as incomplete:
        cleanup_matrix_groups(GroupCli(), azure_context.subscription_id)

    assert incomplete.value.code == "live_matrix_cleanup_incomplete"
    assert attempted == [group["id"] for group in groups]


def test_fixture_creation_cleans_group_after_unexpected_failure(
    monkeypatch,
    tmp_path,
    azure_context,
) -> None:
    options = _options(tmp_path)
    group_id = (
        f"/subscriptions/{options.subscription_id}/resourceGroups/"
        "rg-insights-live-fixture-test"
    )

    class FixtureCli:
        def json(self, arguments, **_kwargs):
            if arguments[:2] == ["group", "exists"]:
                return True
            if arguments[:2] == ["group", "show"]:
                return {"id": group_id}
            raise AssertionError(arguments)

    monkeypatch.setattr(orchestrator, "doctor", lambda *_args: None)
    monkeypatch.setattr(
        live_matrix,
        "select_context",
        lambda *_args: azure_context,
    )
    monkeypatch.setattr(
        live_matrix,
        "provision_scratch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SDK failure")
        ),
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        live_matrix,
        "cleanup_scratch",
        lambda _cli, **kwargs: cleaned.append(kwargs["resource_group_id"]),
    )
    monkeypatch.setattr(
        live_matrix,
        "resource_group_name",
        lambda *_args: "rg-insights-live-fixture-test",
    )

    with pytest.raises(RuntimeError, match="SDK failure"):
        live_matrix._create_fixture(FixtureCli(), options)

    assert cleaned == [group_id]


def test_chained_resume_keeps_carried_forward_cases_passed(
    tmp_path,
) -> None:
    options = _options(tmp_path)
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "status": "failed",
                "fixture_cleanup_status": "not_needed",
                "configuration_fingerprint": (
                    live_matrix._matrix_configuration_fingerprint(options)
                ),
                "cases": [
                    {
                        "case": "scratch-prompt-scheduled",
                        "status": "passed",
                        "cleanup_status": "complete",
                    },
                    {
                        "case": "scratch-hosted-scheduled",
                        "status": "passed_on_resumed_run",
                        "cleanup_status": "complete",
                    },
                    {
                        "case": "scratch-prompt-protected-scheduled",
                        "status": "failed",
                        "cleanup_status": "complete",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert live_matrix._resume_passed_cases(summary, options) == {
        "scratch-prompt-scheduled",
        "scratch-hosted-scheduled",
    }

    value = json.loads(summary.read_text(encoding="utf-8"))
    value["fixture_cleanup_status"] = "failed:OnboardingError"
    summary.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OnboardingError) as unsafe_resume:
        live_matrix._resume_passed_cases(summary, options)
    assert unsafe_resume.value.code == "invalid_live_matrix_resume_cleanup"

    value["fixture_cleanup_status"] = "not_needed"
    value["cases"].append(
        {
            "case": "__matrix__",
            "status": "failed",
            "cleanup_status": "not_needed",
        }
    )
    summary.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OnboardingError) as matrix_failure:
        live_matrix._resume_passed_cases(summary, options)
    assert matrix_failure.value.code == "invalid_live_matrix_resume_failure"

    value["cases"].pop()
    value["configuration_fingerprint"] = "different"
    summary.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OnboardingError) as configuration_mismatch:
        live_matrix._resume_passed_cases(summary, options)
    assert (
        configuration_mismatch.value.code
        == "live_matrix_resume_configuration_mismatch"
    )
