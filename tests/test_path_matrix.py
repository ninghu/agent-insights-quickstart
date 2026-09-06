from __future__ import annotations

import copy
import json
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from insights_onboarding import live_matrix, models, orchestrator
from insights_onboarding.errors import OnboardingError
from insights_onboarding.live_matrix import (
    FALLBACK_CASES,
    SUPPORTED_CASES,
    CleanupManifest,
    FixtureProject,
    LiveMatrixOptions,
    case_config,
    cleanup_matrix_manifest,
    run_live_matrix,
    select_cases,
    supported_case_names,
    validate_case_result,
)
from insights_onboarding.receipts import write_json_atomic

ALL_CASES = (*SUPPORTED_CASES, *FALLBACK_CASES)


def _options(path: Path) -> LiveMatrixOptions:
    return LiveMatrixOptions(
        subscription_id="11111111-1111-1111-1111-111111111111",
        location="westus3",
        model_name="gpt-5.4",
        model_version="2026-03-05",
        model_format="OpenAI",
        model_sku="GlobalStandard",
        model_capacity=30,
        cases=SUPPORTED_CASES,
        output_dir=path,
        ingestion_timeout_seconds=900,
        insights_timeout_seconds=2400,
        execution_id="matrix-execution-one",
    )


def _technical_result(case):
    return {
        "status": "review_pending",
        "agent": {"name": f"owned-{case.agent_type}", "version": "1", "kind": case.agent_type},
        "result_summary": {
            "insight_count": 1,
            "schedule_enabled": False,
            "first_run_trigger": "manual",
            "concrete_prompt_fix_count": int(case.agent_type == "prompt"),
            "concrete_code_fix_count": int(case.agent_type == "hosted"),
        },
        "quality_review": {
            "schema_version": 1,
            "rubric_version": "1.0",
            "status": "ai_review_pending",
            "execution_status": "complete",
            "input_path": "quality-input.json",
            "summary_path": "quality-summary.json",
            "report_path": "quality-report.md",
            "evidence_status": "sufficient",
            "collection_status": "complete",
            "structural_finding_count": 0,
            "human_status": "pending",
            "ai_status": "pending",
            "quality_approved": False,
        },
        "agent_insights_portal_url": (
            "https://ai.azure.com/nextgen/r/context/build/agents/demo/monitor/insights?tid=tenant"
        ),
    }


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_only_prepared_project_cases_are_primary_and_all_excludes_fallbacks():
    assert supported_case_names() == (
        "existing-create-prompt-oneoff",
        "existing-create-hosted-oneoff",
    )
    assert select_cases("all") == SUPPORTED_CASES
    assert select_cases("") == SUPPORTED_CASES
    assert [case.name for case in FALLBACK_CASES] == [
        "scratch-prompt-oneoff", "scratch-hosted-oneoff",
    ]
    assert select_cases("scratch-hosted-oneoff,existing-create-prompt-oneoff") == (
        SUPPORTED_CASES[0], FALLBACK_CASES[1],
    )


@pytest.mark.parametrize(
    "selection",
    ["scratch-prompt-scheduled", "existing-select-prompt-oneoff",
     "existing-create-prompt-protected-scheduled", "all,scratch-prompt-oneoff", "unknown"],
)
def test_legacy_and_unknown_paths_are_not_bug_bash_cases(selection):
    with pytest.raises(OnboardingError, match="all") as failure:
        select_cases(selection)
    assert failure.value.code == "unknown_live_matrix_case"


def test_list_cases_and_help_are_read_only_and_explain_fallbacks(capsys, monkeypatch):
    monkeypatch.setattr(
        live_matrix, "AzureCli",
        lambda: pytest.fail("Read-only help must not initialize Azure CLI."),
    )
    assert live_matrix.main(["--list-cases"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["cases"] == list(supported_case_names())
    assert listing["fallback_cases"] == [case.name for case in FALLBACK_CASES]
    with pytest.raises(SystemExit) as help_exit:
        live_matrix.parse_args(["--help"])
    assert help_exit.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "11 sample requests" in help_text
    assert "one manual Insights run" in help_text
    assert "human acceptance" in help_text
    assert "--cleanup-manifest" in help_text
    assert "--cleanup-only" not in help_text


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.name)
def test_every_matrix_config_creates_only_a_manual_owned_sample(
    case, tmp_path, make_resources, azure_context,
):
    options = _options(tmp_path)
    fixture = FixtureProject(
        "fixture12345", live_matrix._fixture_config(options), azure_context, make_resources(),
    )
    config = case_config(case, options, fixture=fixture)
    assert config.profile == "bug-bash"
    assert config.creates_sample_agent is True
    assert config.scheduling_enabled is False
    assert config.enable_existing_monitor is False
    assert config.agent_name is None
    assert config.invoke_existing_agent is False
    assert config.protected_trace_content is False
    assert config.agent_type == case.agent_type
    if case.mode == "existing":
        assert config.create_sample_agent is True
        assert config.project_resource_id == fixture.resources.project_resource_id


def test_fixture_is_also_manual_and_uses_hosted_infrastructure_only_when_needed(tmp_path):
    options = _options(tmp_path)
    fixture = live_matrix._fixture_config(options)
    assert fixture.profile == "bug-bash"
    assert fixture.scheduling_enabled is False
    assert fixture.agent_type == "hosted"
    prompt_only = live_matrix._fixture_config(replace(options, cases=SUPPORTED_CASES[:1]))
    assert prompt_only.agent_type == "prompt"


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.name)
def test_manual_matrix_plans_keep_eleven_requests_and_never_enable_scheduling(
    case, monkeypatch, tmp_path, make_resources, azure_context, run_id,
):
    options = _options(tmp_path)
    resources = make_resources()
    fixture = FixtureProject(
        "fixture12345", live_matrix._fixture_config(options), azure_context, resources,
    )
    config = case_config(case, options, fixture=fixture)
    if case.mode == "existing":
        monkeypatch.setattr(orchestrator, "resolve_existing", lambda *_args, **_kwargs: resources)
        monkeypatch.setattr(
            orchestrator, "list_app_insights_connections",
            lambda *_args: [{"id": "prepared-connection"}],
        )
        monkeypatch.setattr(
            orchestrator, "_existing_caller_capabilities", lambda **_kwargs: (True, True),
        )
        monkeypatch.setattr(orchestrator, "_role_mutations", lambda *_args: [])
    plan = orchestrator.build_plan(config, context=azure_context, run_id=run_id, cli=object())
    kinds = [mutation["kind"] for mutation in plan.mutations]
    assert plan.expected["first_run_trigger"] == "manual"
    assert plan.expected["monitor_enabled"] is False
    assert plan.expected["traffic"] == {"healthy": 6, "fault": 5, "total": 11}
    assert "create_sample_agent_version" in kinds
    assert "generate_bounded_traffic" in kinds
    assert "run_agent_insights" in kinds
    assert "enable_monitor" not in kinds
    assert "wait_for_scheduled_agent_insights_result" not in kinds


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.name)
def test_technical_completion_never_claims_ai_or_human_quality_approval(case):
    result = _technical_result(case)
    assertion = validate_case_result(case, result)
    assert assertion["execution_status"] == "complete"
    assert assertion["status"] == "review_pending"
    assert assertion["quality_findings"] == []
    assert assertion["quality_review"] == result["quality_review"]
    assert assertion["quality_review"] is not result["quality_review"]
    assert assertion["ai_review_status"] == "not_performed_by_matrix"
    assert assertion["human_review_status"] == "not_collected_by_matrix"


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("count,code", [(0, "empty_insights"), (1, "missing_concrete_fix")])
def test_empty_and_prose_only_are_preserved_quality_findings_not_execution_failures(
    case, count, code,
):
    result = _technical_result(case)
    result["result_summary"].update(
        insight_count=count, concrete_prompt_fix_count=0, concrete_code_fix_count=0,
    )
    assertion = validate_case_result(case, result)
    assert assertion["execution_status"] == "complete"
    assert assertion["status"] == "review_pending"
    assert assertion["insight_count"] == count
    assert assertion["quality_findings"][0]["code"] == code
    assert assertion["quality_findings"][0]["category"] == "quality"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("first_run_trigger", "scheduled", "live_matrix_trigger_mismatch"),
        ("schedule_enabled", True, "live_matrix_schedule_mismatch"),
        ("schedule_enabled", "", "live_matrix_schedule_mismatch"),
        ("insight_count", -1, "live_matrix_invalid_result"),
        ("insight_count", True, "live_matrix_invalid_result"),
        ("concrete_prompt_fix_count", "1", "live_matrix_invalid_result"),
    ],
)
def test_execution_contract_errors_still_fail(field, value, code):
    result = _technical_result(SUPPORTED_CASES[0])
    result["result_summary"][field] = value
    with pytest.raises(OnboardingError) as failure:
        validate_case_result(SUPPORTED_CASES[0], result)
    assert failure.value.code == code


def test_quality_module_findings_and_evidence_states_are_not_hidden():
    result = _technical_result(SUPPORTED_CASES[0])
    result["quality_review"].update(
        structural_finding_count=3,
        evidence_status="insufficient_evidence",
        collection_status="partial",
    )
    assertion = validate_case_result(SUPPORTED_CASES[0], result)
    assert assertion["quality_findings"] == [
        {"code": "review_structural_findings", "category": "quality", "count": 3},
    ]
    assert assertion["evidence_status"] == "insufficient_evidence"
    assert assertion["collection_status"] == "partial"
    assert assertion["human_review_status"] == "not_collected_by_matrix"


class MemoryCli:
    def __init__(self):
        self.groups = {}
        self.calls = []
        self.deleted = []

    def json(self, arguments, **_kwargs):
        self.calls.append(arguments)
        assert arguments[:2] in (["group", "exists"], ["group", "show"])
        name = arguments[arguments.index("--name") + 1]
        if arguments[1] == "exists":
            return name in self.groups
        return self.groups[name]


@pytest.fixture
def matrix_environment(monkeypatch, tmp_path, azure_context, make_resources):
    cli = MemoryCli()
    options = _options(tmp_path / "matrix")
    fixture_configs = []
    onboard_configs = []
    monkeypatch.setattr(live_matrix, "select_context", lambda *_args: azure_context)
    monkeypatch.setattr(
        live_matrix, "_source_provenance",
        lambda: {"git_commit": "a" * 40, "content_sha256": "b" * 64, "files": []},
    )
    monkeypatch.setattr(orchestrator, "_run_dir", lambda run: tmp_path / "runs" / run)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_args: {"status": "ready"})

    def group(config, run):
        name = live_matrix.resource_group_name(config, run)
        value = {
            "id": f"/subscriptions/{azure_context.subscription_id}/resourceGroups/{name}",
            "name": name,
            "tags": {
                "created-by": "agent-insights-quickstart",
                "run-id": run,
                "owner-object-id": azure_context.user_object_id,
            },
        }
        cli.groups[name] = value
        return value

    def provision(_cli, *, config, context, run_id, resource_observer):
        fixture_configs.append(config)
        value = group(config, run_id)
        resource_observer("resource_group", value)
        return make_resources(resource_group_id=value["id"])

    def onboard(config, **kwargs):
        onboard_configs.append(config)
        kwargs["progress_callback"]({"stage": "provisioning"})
        if config.mode == "scratch":
            value = group(config, kwargs["run_id"])
            kwargs["resource_observer"]("resource_group", value)
        kwargs["progress_callback"]({"stage": "insights_running"})
        case = next(
            case for case in ALL_CASES
            if case.mode == config.mode and case.agent_type == config.agent_type
        )
        result = _technical_result(case)
        result["run_id"] = kwargs["run_id"]
        return result

    def cleanup(_cli, *, resource_group_id, run_id, owner_object_id):
        assert owner_object_id == azure_context.user_object_id
        assert resource_group_id.endswith(run_id)
        cli.deleted.append(resource_group_id)
        del cli.groups[resource_group_id.rsplit("/", 1)[1]]

    monkeypatch.setattr(live_matrix, "provision_scratch", provision)
    monkeypatch.setattr(orchestrator, "onboard", onboard)
    monkeypatch.setattr(live_matrix, "cleanup_scratch", cleanup)
    return SimpleNamespace(
        cli=cli, options=options, group=group, context=azure_context,
        provision=provision, onboard=onboard, cleanup=cleanup,
        fixture_configs=fixture_configs, onboard_configs=onboard_configs,
    )


def test_primary_matrix_prepares_one_owned_fixture_and_records_both_manual_results(
    matrix_environment,
):
    env = matrix_environment
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "review_pending"
    assert summary["execution_status"] == "complete"
    assert summary["completed_case_count"] == summary["expected_case_count"] == 2
    assert summary["cleanup_status"] == "complete"
    assert summary["current_case"] is None
    assert summary["stage"] == "finished"
    assert summary["human_review_status"] == "not_collected_by_matrix"
    assert [item["case"] for item in summary["cases"]] == list(supported_case_names())
    assert len(env.fixture_configs) == 1
    assert len(env.onboard_configs) == 2
    assert all(config.profile == "bug-bash" for config in env.onboard_configs)
    assert all(config.mode == "existing" for config in env.onboard_configs)
    manifest = _read(env.options.output_dir / "cleanup-manifest.json")
    assert len(manifest["runs"]) == 3
    assert len(manifest["resource_groups"]) == len(env.cli.deleted) == 1
    assert manifest["cleanup_status"] == "complete"
    assert manifest["resource_groups"][0]["cleanup_status"] == "deleted"
    assert not env.cli.groups
    assert _read(env.options.output_dir / "summary.json") == summary


def test_explicit_scratch_fallbacks_do_not_create_shared_existing_fixture(matrix_environment):
    env = matrix_environment
    summary = run_live_matrix(replace(env.options, cases=FALLBACK_CASES), cli=env.cli)
    assert summary["execution_status"] == "complete"
    assert not env.fixture_configs
    assert len(env.onboard_configs) == 2
    assert all(config.mode == "scratch" for config in env.onboard_configs)
    assert len(env.cli.deleted) == 2
    manifest = _read(env.options.output_dir / "cleanup-manifest.json")
    assert all(run["case"] != "__fixture__" for run in manifest["runs"])


def test_matrix_quality_findings_remain_visible_without_failing_technical_execution(
    matrix_environment, monkeypatch,
):
    env = matrix_environment

    def no_fixes(config, **kwargs):
        result = env.onboard(config, **kwargs)
        result["result_summary"].update(concrete_prompt_fix_count=0, concrete_code_fix_count=0)
        return result

    monkeypatch.setattr(orchestrator, "onboard", no_fixes)
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "review_pending"
    assert summary["execution_status"] == "complete"
    assert summary["quality_status"] == "findings"
    assert all(
        result["assertion"]["quality_findings"][0]["code"] == "missing_concrete_fix"
        for result in summary["cases"]
    )


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_second_case_keeps_partial_accounting_and_cleanup_evidence(
    matrix_environment, monkeypatch, interrupt, capsys,
):
    env = matrix_environment
    calls = []

    def interrupted(config, **kwargs):
        calls.append(config)
        if len(calls) == 1:
            return env.onboard(config, **kwargs)
        kwargs["progress_callback"]({"stage": "insights_running"})
        checkpoint = _read(env.options.output_dir / "summary.json")
        assert checkpoint["status"] == "running"
        assert checkpoint["completed_case_count"] == 1
        assert checkpoint["expected_case_count"] == 2
        assert checkpoint["current_case"] == SUPPORTED_CASES[1].name
        assert checkpoint["stage"] == "insights_running"
        raise interrupt()

    monkeypatch.setattr(orchestrator, "onboard", interrupted)
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "cancelled"
    assert summary["execution_status"] == "cancelled"
    assert summary["completed_case_count"] == 1
    assert summary["expected_case_count"] == 2
    assert summary["cases"][1]["status"] == "cancelled"
    assert summary["cases"][1]["run_id"]
    assert summary["cases"][1]["error"]["stage"] == "insights_running"
    assert summary["cleanup_status"] == "complete"
    assert not env.cli.groups
    assert _read(env.options.output_dir / "summary.json") == summary
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["completed_case_count"] == 1
    assert any(event["stage"] == "cancelled" for event in events)


def test_fixture_group_is_manifested_before_late_provisioning_failure(
    matrix_environment, monkeypatch,
):
    env = matrix_environment

    def fail_after_group(cli, **kwargs):
        env.provision(cli, **kwargs)
        manifest = _read(env.options.output_dir / "cleanup-manifest.json")
        assert manifest["resource_groups"][0]["run_id"] == kwargs["run_id"]
        assert manifest["runs"][0]["resource_group_observed"] is True
        raise RuntimeError("later deployment failed")

    monkeypatch.setattr(live_matrix, "provision_scratch", fail_after_group)
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "failed"
    assert summary["completed_case_count"] == 0
    assert summary["cleanup_status"] == "complete"
    assert summary["matrix_error"]["exception_type"] == "RuntimeError"
    assert summary["matrix_error"]["stage"] == "fixture_provisioning"
    assert len(env.cli.deleted) == 1
    assert not env.cli.groups


def test_unobserved_creation_fails_cleanup_instead_of_guessing_group_addresses(
    matrix_environment, monkeypatch,
):
    env = matrix_environment

    def uncertain(*_args, **_kwargs):
        raise RuntimeError("group creation outcome was not observed")

    monkeypatch.setattr(live_matrix, "provision_scratch", uncertain)
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "failed"
    assert summary["cleanup_status"] == "failed"
    assert not env.cli.calls
    manifest = _read(env.options.output_dir / "cleanup-manifest.json")
    assert manifest["cleanup_failures"][0]["error_code"] == "resource_creation_not_observed"


def test_preflight_failure_before_group_creation_has_no_unobserved_mutation(
    matrix_environment, monkeypatch,
):
    env = matrix_environment

    def blocked(*_args):
        raise OnboardingError("preflight_blocked", "No mutations were made.")

    monkeypatch.setattr(orchestrator, "doctor", blocked)
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "failed"
    assert summary["cleanup_status"] == "complete"
    assert summary["matrix_error"]["code"] == "preflight_blocked"
    assert not env.cli.calls
    manifest = _read(env.options.output_dir / "cleanup-manifest.json")
    assert manifest["runs"][0]["creation_started"] is False


def test_interrupted_cleanup_is_not_success_and_independent_manifest_remains_usable(
    matrix_environment, monkeypatch,
):
    env = matrix_environment
    original = live_matrix.cleanup_matrix_manifest

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(live_matrix, "cleanup_matrix_manifest", interrupted)
    summary = run_live_matrix(env.options, cli=env.cli)
    assert summary["status"] == "cancelled"
    assert summary["cleanup_status"] == "cancelled"
    assert env.cli.groups
    result = original(
        env.cli, env.options.output_dir / "cleanup-manifest.json",
        execution_id=env.options.execution_id, subscription_id=env.options.subscription_id,
    )
    assert result["status"] == "complete"
    assert not env.cli.groups


def test_sigterm_maps_to_cancelled_exception_and_handler_is_restored():
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), live_matrix._cancellation_handler():
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.parametrize("count", [0, 1])
def test_zero_or_partial_finished_summary_cannot_be_complete(matrix_environment, count):
    env = matrix_environment
    results = [{
        "case": SUPPORTED_CASES[0].name, "status": "review_pending",
        "execution_status": "complete", "assertion": {},
    }][:count]
    summary = live_matrix._summary_payload(
        results, 0, env.options, provenance=live_matrix._matrix_provenance(env.options),
        state="finished", stage="finished", current_case=None, cleanup_status="complete",
    )
    assert summary["status"] == "incomplete"
    assert summary["completed_case_count"] == count
    assert summary["expected_case_count"] == 2


def test_empty_case_selection_is_rejected_before_azure(matrix_environment):
    env = matrix_environment
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(replace(env.options, cases=()), cli=env.cli)
    assert failure.value.code == "invalid_live_matrix_cases"
    assert not env.cli.calls


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_unbounded_or_nonpositive_timeouts_are_rejected_before_azure(matrix_environment, timeout):
    env = matrix_environment
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(replace(env.options, insights_timeout_seconds=timeout), cli=env.cli)
    assert failure.value.code == "invalid_live_matrix_timeout"
    assert not env.onboard_configs


def _observed_manifest(env, path, cases=FALLBACK_CASES[:1]):
    options = replace(env.options, cases=cases)
    manifest = CleanupManifest(path, options, env.context, "a" * 64)
    for index, case in enumerate(cases):
        run_id = f"abc123def45{index}"
        config = case_config(case, options)
        manifest.register(run_id, case.name, resource_group_expected=True)
        manifest.observer(run_id, config)("resource_group", env.group(config, run_id))
    return manifest


def _cleanup(env, path, **overrides):
    kwargs = {
        "execution_id": env.options.execution_id,
        "subscription_id": env.options.subscription_id,
        **overrides,
    }
    return cleanup_matrix_manifest(env.cli, path, **kwargs)


def test_cleanup_deletes_only_this_execution_not_other_active_runs(matrix_environment, tmp_path):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    other = env.group(case_config(FALLBACK_CASES[0], env.options), "def456abc789")
    result = _cleanup(env, path)
    assert result["status"] == "complete"
    assert result["resource_group_count"] == 1
    assert list(env.cli.groups) == [other["name"]]
    assert len(env.cli.deleted) == 1
    assert all(call[:2] != ["group", "list"] for call in env.cli.calls)


def test_wrong_runner_identity_explicitly_fails_before_any_group_query(
    matrix_environment, tmp_path, monkeypatch,
):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    different = replace(env.context, user_object_id="55555555-5555-5555-5555-555555555555")
    monkeypatch.setattr(live_matrix, "select_context", lambda *_args: different)
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "live_matrix_cleanup_identity_mismatch"
    assert not env.cli.calls
    assert not env.cli.deleted


@pytest.mark.parametrize(
    "override",
    [
        {"execution_id": "another-execution"},
        {"subscription_id": "77777777-7777-7777-7777-777777777777"},
    ],
)
def test_cross_execution_or_subscription_manifest_is_rejected(
    matrix_environment, tmp_path, override,
):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path, **override)
    assert failure.value.code == "invalid_cleanup_manifest"
    assert not env.cli.calls


def test_manifest_digest_detects_modified_targets(matrix_environment, tmp_path):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    value = _read(path)
    value["resource_groups"][0]["resource_group_id"] += "-different"
    write_json_atomic(path, value)
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "invalid_cleanup_manifest"
    assert not env.cli.calls


@pytest.mark.parametrize("field", ["resource_group_id", "name", "run_id", "owner_object_id"])
def test_resealed_manifest_still_cannot_target_another_group_or_owner(
    matrix_environment, tmp_path, field,
):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    value = _read(path)
    value["resource_groups"][0][field] = "another-resource"
    write_json_atomic(path, live_matrix._seal(value, "manifest_sha256"))
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "invalid_cleanup_manifest"
    assert not env.cli.calls


@pytest.mark.parametrize("tag", ["run-id", "owner-object-id", "created-by"])
def test_cleanup_rechecks_live_ownership_tags(matrix_environment, tmp_path, tag):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    next(iter(env.cli.groups.values()))["tags"][tag] = "different"
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "live_matrix_cleanup_incomplete"
    assert failure.value.details["failures"][0]["error_code"] == "ownership_mismatch"
    assert not env.cli.deleted
    assert _read(path)["cleanup_status"] == "failed"


def test_cleanup_checks_live_group_id_not_just_its_name(matrix_environment, tmp_path):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    next(iter(env.cli.groups.values()))["id"] += "-other"
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "live_matrix_cleanup_incomplete"
    assert not env.cli.deleted


def test_manifest_cleanup_is_idempotent_for_previously_observed_absent_groups(
    matrix_environment, tmp_path,
):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    assert _cleanup(env, path)["status"] == "complete"
    second = _cleanup(env, path)
    assert second["status"] == "complete"
    assert second["resource_groups"][0]["cleanup_status"] == "already_absent"
    assert len(env.cli.deleted) == 1


def test_cleanup_attempts_every_manifested_group_even_if_first_fails(
    matrix_environment, tmp_path, monkeypatch,
):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path, FALLBACK_CASES)
    attempted = []

    def fail_first(cli, **kwargs):
        attempted.append(kwargs["run_id"])
        if len(attempted) == 1:
            raise RuntimeError("first deletion failed")
        env.cleanup(cli, **kwargs)

    monkeypatch.setattr(live_matrix, "cleanup_scratch", fail_first)
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "live_matrix_cleanup_incomplete"
    assert len(attempted) == 2
    manifest = _read(path)
    assert [group["cleanup_status"] for group in manifest["resource_groups"]] == [
        "failed", "deleted",
    ]


def test_missing_manifest_is_failure_not_success(matrix_environment, tmp_path):
    with pytest.raises(OnboardingError) as failure:
        _cleanup(matrix_environment, tmp_path / "missing.json")
    assert failure.value.code == "invalid_cleanup_manifest"
    assert not matrix_environment.cli.calls


def test_cleanup_cli_requires_both_opt_in_and_execution_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(live_matrix, "AzureCli", lambda: pytest.fail("No Azure access expected."))
    with pytest.raises(OnboardingError) as missing_opt_in:
        live_matrix.main(["--cleanup-manifest", str(tmp_path / "cleanup.json")])
    assert missing_opt_in.value.code == "live_confirmation_required"
    with pytest.raises(OnboardingError) as missing_execution:
        live_matrix.main([
            "--confirm-live", "--cleanup-manifest", str(tmp_path / "cleanup.json"),
            "--subscription-id", "11111111-1111-1111-1111-111111111111",
        ])
    assert missing_execution.value.code == "live_matrix_configuration_missing"


def test_resume_preserves_original_assertions_provenance_and_pending_human_state(
    matrix_environment, tmp_path,
):
    env = matrix_environment
    first = run_live_matrix(env.options, cli=env.cli)
    resumed_options = replace(
        env.options, output_dir=tmp_path / "resumed", execution_id="matrix-execution-two",
        resume_summary=env.options.output_dir / "summary.json",
    )
    resumed = run_live_matrix(resumed_options, cli=env.cli)
    assert resumed["execution_status"] == "complete"
    assert len(env.onboard_configs) == 2
    assert len(env.fixture_configs) == 1
    for old, new in zip(first["cases"], resumed["cases"], strict=True):
        assert new["assertion"] == old["assertion"]
        assert new["provenance"] == old["provenance"]
        assert new["run_id"] == old["run_id"]
        assert new["reused_from"]["execution_id"] == first["execution_id"]
        assert new["reused_from"]["summary_sha256"] == first["summary_sha256"]
    assert resumed["human_review_status"] == "not_collected_by_matrix"
    assert not _read(resumed_options.output_dir / "cleanup-manifest.json")["resource_groups"]


def test_chained_resume_keeps_original_evidence(matrix_environment, tmp_path):
    env = matrix_environment
    first = run_live_matrix(env.options, cli=env.cli)
    second_options = replace(
        env.options, output_dir=tmp_path / "second", execution_id="matrix-execution-two",
        resume_summary=env.options.output_dir / "summary.json",
    )
    second = run_live_matrix(second_options, cli=env.cli)
    third = run_live_matrix(
        replace(
            second_options, output_dir=tmp_path / "third", execution_id="matrix-execution-three",
            resume_summary=second_options.output_dir / "summary.json",
        ),
        cli=env.cli,
    )
    assert third["cases"][0]["assertion"] == first["cases"][0]["assertion"]
    assert third["cases"][0]["provenance"] == first["cases"][0]["provenance"]
    assert third["cases"][0]["reused_from"]["execution_id"] == second["execution_id"]
    assert len(third["cases"][0]["reuse_history"]) == 2
    assert len(env.onboard_configs) == 2


def test_resume_after_cancellation_only_creates_a_fresh_attempt_for_unfinished_case(
    matrix_environment, monkeypatch, tmp_path,
):
    env = matrix_environment

    def interrupt_hosted(config, **kwargs):
        if config.agent_type == "hosted":
            raise KeyboardInterrupt
        return env.onboard(config, **kwargs)

    monkeypatch.setattr(orchestrator, "onboard", interrupt_hosted)
    first = run_live_matrix(env.options, cli=env.cli)
    assert first["status"] == "cancelled"
    assert first["cleanup_status"] == "complete"
    monkeypatch.setattr(orchestrator, "onboard", env.onboard)
    resumed = run_live_matrix(
        replace(
            env.options, output_dir=tmp_path / "resumed", execution_id="matrix-resumed",
            resume_summary=env.options.output_dir / "summary.json",
        ),
        cli=env.cli,
    )
    assert resumed["execution_status"] == "complete"
    assert [config.agent_type for config in env.onboard_configs] == ["prompt", "hosted"]
    assert resumed["cases"][0]["run_id"] == first["cases"][0]["run_id"]
    assert resumed["cases"][1]["run_id"] != first["cases"][1]["run_id"]


def test_resume_keeps_poor_quality_output_instead_of_retrying_for_a_better_score(
    matrix_environment, monkeypatch, tmp_path,
):
    env = matrix_environment

    def empty_insights(config, **kwargs):
        result = env.onboard(config, **kwargs)
        result["result_summary"].update(
            insight_count=0, concrete_prompt_fix_count=0, concrete_code_fix_count=0,
        )
        return result

    monkeypatch.setattr(orchestrator, "onboard", empty_insights)
    first = run_live_matrix(env.options, cli=env.cli)
    resumed = run_live_matrix(
        replace(
            env.options, output_dir=tmp_path / "resumed", execution_id="matrix-resumed",
            resume_summary=env.options.output_dir / "summary.json",
        ),
        cli=env.cli,
    )
    assert resumed["quality_status"] == "findings"
    assert resumed["cases"][0]["assertion"] == first["cases"][0]["assertion"]
    assert resumed["cases"][0]["assertion"]["quality_findings"][0]["code"] == "empty_insights"
    assert len(env.onboard_configs) == 2


@pytest.mark.parametrize(
    "insight_count, finding", [(0, "empty_insights"), (1, "missing_concrete_fix")],
)
@pytest.mark.parametrize("cleanup_failure", ["timeout", "interrupted"])
def test_resume_carries_completed_quality_evidence_after_case_cleanup_failure(
    matrix_environment, monkeypatch, tmp_path, insight_count, finding, cleanup_failure,
):
    env = matrix_environment
    options = replace(env.options, cases=SUPPORTED_CASES[:1])
    cleanup_calls = []

    def quality_result(config, **kwargs):
        result = env.onboard(config, **kwargs)
        result["result_summary"].update(
            insight_count=insight_count,
            concrete_prompt_fix_count=0,
            concrete_code_fix_count=0,
        )
        return result

    def failed_case_cleanup(run_id, **_kwargs):
        cleanup_calls.append(run_id)
        if cleanup_failure == "interrupted":
            raise KeyboardInterrupt
        raise OnboardingError("azure_cli_timeout", "Case cleanup timed out.")

    monkeypatch.setattr(orchestrator, "onboard", quality_result)
    monkeypatch.setattr(live_matrix, "_cleanup_case", failed_case_cleanup)
    first = run_live_matrix(options, cli=env.cli)
    original = first["cases"][0]
    assert original["execution_status"] == "complete"
    assert original["error"] is None
    assert original["cleanup_status"] == (
        "cancelled" if cleanup_failure == "interrupted" else "failed"
    )
    assert original["cleanup_error"]["code"] == (
        "live_matrix_cancelled" if cleanup_failure == "interrupted" else "azure_cli_timeout"
    )
    assert original["assertion"]["quality_findings"][0]["code"] == finding
    assert first["cleanup_status"] == "complete"
    manifest = _read(options.output_dir / "cleanup-manifest.json")
    assert manifest["cleanup_status"] == "complete"
    assert manifest["resource_groups"][0]["cleanup_status"] == "deleted"
    assert not env.cli.groups

    monkeypatch.setattr(
        orchestrator, "onboard",
        lambda *_args, **_kwargs: pytest.fail("Completed evidence must not rerun a sample."),
    )
    resumed = run_live_matrix(
        replace(
            options, output_dir=tmp_path / "resumed", execution_id="matrix-resumed",
            resume_summary=options.output_dir / "summary.json",
        ),
        cli=env.cli,
    )
    retained = resumed["cases"][0]
    assert resumed["execution_status"] == "complete"
    assert retained["execution_status"] == "complete"
    assert retained["run_id"] == original["run_id"]
    assert retained["assertion"] == original["assertion"]
    assert retained["provenance"] == original["provenance"]
    assert retained["cleanup_status"] == original["cleanup_status"]
    assert retained["cleanup_error"] == original["cleanup_error"]
    assert len(env.onboard_configs) == len(env.fixture_configs) == len(cleanup_calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"model_capacity": 1},
        {"cases": SUPPORTED_CASES[:1]},
        {"ingestion_timeout_seconds": 10},
        {"location": "eastus"},
    ],
)
def test_resume_rejects_changed_selection_and_config(matrix_environment, tmp_path, changes):
    env = matrix_environment
    run_live_matrix(env.options, cli=env.cli)
    options = replace(
        env.options, output_dir=tmp_path / "resumed",
        resume_summary=env.options.output_dir / "summary.json", **changes,
    )
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(options, cli=env.cli)
    assert failure.value.code == "live_matrix_resume_configuration_mismatch"
    assert len(env.onboard_configs) == 2


def test_resume_rejects_dirty_source_change_even_at_same_head(
    matrix_environment, tmp_path, monkeypatch,
):
    env = matrix_environment
    run_live_matrix(env.options, cli=env.cli)
    monkeypatch.setattr(
        live_matrix, "_source_provenance",
        lambda: {"git_commit": "a" * 40, "content_sha256": "c" * 64, "files": []},
    )
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(
            replace(
                env.options, output_dir=tmp_path / "resumed",
                resume_summary=env.options.output_dir / "summary.json",
            ),
            cli=env.cli,
        )
    assert failure.value.code == "live_matrix_resume_configuration_mismatch"


@pytest.mark.parametrize("state", ["running", "cleanup_failed"])
def test_resume_rejects_unfinished_or_uncleaned_evidence(
    matrix_environment, tmp_path, state,
):
    env = matrix_environment
    summary = run_live_matrix(env.options, cli=env.cli)
    if state == "running":
        summary["status"] = "running"
    else:
        summary["cleanup_status"] = "failed"
    path = env.options.output_dir / "summary.json"
    write_json_atomic(path, live_matrix._seal(summary, "summary_sha256"))
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(
            replace(env.options, output_dir=tmp_path / "resumed", resume_summary=path),
            cli=env.cli,
        )
    assert failure.value.code in {
        "invalid_live_matrix_resume", "invalid_live_matrix_resume_cleanup",
    }


def test_modified_resume_assertions_require_matching_digest(matrix_environment, tmp_path):
    env = matrix_environment
    summary = run_live_matrix(env.options, cli=env.cli)
    summary["cases"][0]["assertion"]["insight_count"] = 999
    path = env.options.output_dir / "summary.json"
    write_json_atomic(path, summary)
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(
            replace(env.options, output_dir=tmp_path / "resumed", resume_summary=path),
            cli=env.cli,
        )
    assert failure.value.code == "invalid_live_matrix_resume"


def test_same_output_directory_cannot_overwrite_manifest_or_summary(matrix_environment):
    env = matrix_environment
    first = run_live_matrix(env.options, cli=env.cli)
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(env.options, cli=env.cli)
    assert failure.value.code == "live_matrix_output_exists"
    assert _read(env.options.output_dir / "summary.json") == first


def test_even_an_empty_reserved_directory_cannot_be_shared_between_executions(matrix_environment):
    env = matrix_environment
    env.options.output_dir.mkdir(parents=True)
    with pytest.raises(OnboardingError) as failure:
        run_live_matrix(env.options, cli=env.cli)
    assert failure.value.code == "live_matrix_output_exists"
    assert not env.onboard_configs


@pytest.mark.parametrize("case", [None, [], {}])
def test_malformed_manifest_case_is_rejected_before_azure(
    matrix_environment, tmp_path, case,
):
    env = matrix_environment
    path = tmp_path / "cleanup.json"
    _observed_manifest(env, path)
    value = _read(path)
    value["runs"][0]["case"] = case
    write_json_atomic(path, live_matrix._seal(value, "manifest_sha256"))
    with pytest.raises(OnboardingError) as failure:
        _cleanup(env, path)
    assert failure.value.code == "invalid_cleanup_manifest"
    assert not env.cli.calls


@pytest.mark.parametrize(
    "relative",
    [
        ".agents/skills/agent-insights-onboarding/scripts/insights_onboarding/live_matrix.py",
        ".agents/skills/agent-insights-onboarding/scripts/insights_onboarding/quality_review.py",
        ".agents/skills/agent-insights-onboarding/assets/agents/manifest.json",
        ".agents/skills/agent-insights-onboarding/references/quality-review.md",
        "requirements-dev.lock",
        "pyproject.toml",
    ],
)
def test_source_digest_covers_dirty_code_baselines_rubric_locks_and_config(
    tmp_path, monkeypatch, relative,
):
    target = tmp_path.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(
        live_matrix.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="a" * 40),
    )
    before = live_matrix._source_provenance(tmp_path)
    target.write_text("changed without a commit", encoding="utf-8")
    after = live_matrix._source_provenance(tmp_path)
    assert before["git_commit"] == after["git_commit"]
    assert before["content_sha256"] != after["content_sha256"]
    assert len(after["files"]) == 1


def test_fingerprint_changes_with_case_definition_profile_and_schema(
    matrix_environment, monkeypatch,
):
    env = matrix_environment
    before = live_matrix._matrix_configuration_fingerprint(env.options)
    changed_case = replace(SUPPORTED_CASES[0], agent_type="hosted")
    assert before != live_matrix._matrix_configuration_fingerprint(
        replace(env.options, cases=(changed_case, SUPPORTED_CASES[1])),
    )
    monkeypatch.setattr(live_matrix, "MATRIX_SCHEMA_VERSION", 999)
    assert before != live_matrix._matrix_configuration_fingerprint(env.options)
    monkeypatch.setattr(live_matrix, "MATRIX_SCHEMA_VERSION", 2)
    monkeypatch.setattr(live_matrix, "PROFILE", "another-profile")
    assert before != live_matrix._matrix_configuration_fingerprint(env.options)


def test_legacy_standard_profile_scheduling_remains_separate_from_bug_bash_matrix():
    config = models.OnboardingConfig(
        mode="scratch",
        subscription_id="11111111-1111-1111-1111-111111111111",
        location="westus3",
        agent_type="prompt",
    )
    assert config.profile == "standard"
    assert config.scheduling_enabled is True
    assert replace(config, profile="bug-bash").scheduling_enabled is False


def test_workflow_is_opt_in_streamed_windows_and_uses_only_execution_manifest(repo_root):
    workflow = (repo_root / ".github" / "workflows" / "live-matrix.yml").read_text(encoding="utf-8")
    live, cleanup = workflow.split("\n  cleanup:\n")
    assert "schedule:" in live
    assert "workflow_dispatch:" in live
    assert "AGENT_INSIGHTS_LIVE_ENABLED == 'true'" in live
    assert "AGENT_INSIGHTS_LIVE_ENABLED == 'true'" in cleanup
    assert "cancel-in-progress: false" in live
    for job in (live, cleanup):
        assert "- self-hosted" in job
        assert "- Windows" in job
        assert "- agent-insights-live" in job
        assert "user.type" in job
        assert "--execution-id $env:MATRIX_EXECUTION_ID" in job
        assert "include-hidden-files: true" in job
        assert "github.run_attempt" in job
    assert "$output = python" not in workflow
    assert "python -u" in live
    assert "2>&1 | ForEach-Object { Write-Host $_ }" in live
    assert "$exitCode = $LASTEXITCODE" in live
    assert "exit $exitCode" in live
    assert "--cleanup-only" not in workflow
    assert "Remove stale" not in workflow
    assert "--cleanup-manifest $manifest" in cleanup
    assert "MATRIX_EXECUTION_ID: ${{ needs.live-matrix.outputs.execution_id }}" in cleanup
    assert "manifest_artifact: ${{ steps.identity.outputs.manifest_artifact }}" in live
    assert "name: ${{ env.MATRIX_MANIFEST_ARTIFACT }}" in live
    assert "name: ${{ env.MATRIX_MANIFEST_ARTIFACT }}" in cleanup
    assert "actions/download-artifact@v6" in cleanup
    assert "always()" in cleanup
    assert "if-no-files-found: error" in live
    assert "path: .agent-insights/live-matrix/**" not in workflow
    assert "azure/login" not in workflow


def test_fixture_observer_rejects_nonowned_resource_before_persisting_target(
    matrix_environment, tmp_path,
):
    env = matrix_environment
    manifest = CleanupManifest(tmp_path / "manifest.json", env.options, env.context, "a" * 64)
    run = "abc123def456"
    config = case_config(FALLBACK_CASES[0], env.options)
    manifest.register(run, FALLBACK_CASES[0].name, resource_group_expected=True)
    value = copy.deepcopy(env.group(config, run))
    value["tags"]["owner-object-id"] = "another-user"
    with pytest.raises(OnboardingError) as failure:
        manifest.observer(run, config)("resource_group", value)
    assert failure.value.code == "ownership_mismatch"
    assert not _read(manifest.path)["resource_groups"]
