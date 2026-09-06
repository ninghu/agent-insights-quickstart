from __future__ import annotations

from dataclasses import asdict

import pytest
from insights_onboarding import orchestrator
from insights_onboarding.errors import OnboardingError
from insights_onboarding.models import OnboardingPlan, ProjectResources
from insights_onboarding.permissions import MONITORING_READER, RequiredAssignment
from insights_onboarding.receipts import read_json, write_json_atomic


def test_prepare_project_has_no_agents_traffic_or_monitors(
    monkeypatch, tmp_path, make_config, make_resources, azure_context, run_id
):
    config = make_config(profile="bug-bash", agent_type="hosted")
    resources = make_resources()
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "select_context", lambda *_a: azure_context)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_a: {"status": "ready"})
    calls = []

    def provision(*_args, resource_observer, **_kwargs):
        calls.append("provision")
        resource_observer("resource_group", {"id": resources.resource_group_id})
        return resources

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Infrastructure preparation must not create Agents or traffic.")

    monkeypatch.setattr(orchestrator, "provision_scratch", provision)
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_a, **_k: ())
    monkeypatch.setattr(orchestrator, "_wait_for_authorization", lambda **_k: None)
    monkeypatch.setattr(orchestrator, "create_sample_agent", forbidden)
    monkeypatch.setattr(orchestrator, "generate_sample_traffic", forbidden)
    monkeypatch.setattr(orchestrator, "_complete_monitor", forbidden)
    result = orchestrator.prepare_project(config, run_id=run_id, cli=object())
    assert result["status"] == "project_ready"
    assert result["traffic_generated"] == 0
    assert result["project"]["project_endpoint"] == resources.project_endpoint
    plan = read_json(tmp_path / run_id / "plan.json")
    assert {item["kind"] for item in plan["mutations"]} == {
        "create_resource_group", "deploy_scratch_environment",
    }
    assert plan["expected"]["operation"] == "prepare_project"
    assert orchestrator.prepare_project(config, run_id=run_id, cli=object()) == result
    assert calls == ["provision"]
    with pytest.raises(OnboardingError) as wrong_operation:
        orchestrator.onboard(config, run_id=run_id, cli=object())
    assert wrong_operation.value.code == "infrastructure_only_run"


def test_prepare_project_dry_run_is_read_only(
    monkeypatch, tmp_path, make_config, azure_context, run_id
):
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "select_context", lambda *_a: azure_context)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_a: {"status": "ready"})
    result = orchestrator.prepare_project(
        make_config(profile="bug-bash"), run_id=run_id, cli=object(), dry_run=True
    )
    assert result["status"] == "planned_no_writes"
    assert not (tmp_path / run_id / "provisioning-state.json").exists()


def test_prepare_project_rejects_other_modes_and_profiles(make_config):
    for config in (make_config(), make_config(mode="existing", profile="bug-bash")):
        with pytest.raises(OnboardingError) as invalid:
            orchestrator.prepare_project(config, cli=object())
        assert invalid.value.code == "invalid_project_preparation"


def test_access_refresh_only_grants_the_missing_component_reader(
    monkeypatch, tmp_path, make_config, make_resources, azure_context, run_id
):
    config = make_config(profile="bug-bash", agent_type="hosted")
    group_name = orchestrator.resource_group_name(config, run_id)
    original = asdict(make_resources())
    resources = ProjectResources(**{
        key: value.replace("rg-agent-insights", group_name) if isinstance(value, str) else value
        for key, value in original.items()
    })
    plan = OnboardingPlan.create(
        run_id=run_id, config=config, context=azure_context, mutations=[],
        expected={"operation": "prepare_project"},
    ).as_dict()
    run_dir = tmp_path / run_id
    write_json_atomic(run_dir / "plan.json", plan)
    write_json_atomic(run_dir / "final-receipt.json", {
        "status": "project_ready", "project": asdict(resources),
    })
    target = RequiredAssignment(
        resources.project_principal_id, "ServicePrincipal", MONITORING_READER,
        resources.application_insights_resource_id,
    )
    calls = []

    class OwnedCli:
        def json(self, arguments):
            assert arguments == ["group", "show", "--name", group_name]
            return {
                "id": resources.resource_group_id,
                "tags": {
                    "created-by": "agent-insights-quickstart", "run-id": run_id,
                    "owner-object-id": azure_context.user_object_id,
                },
            }

    missing = iter(([target], []))
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "select_context", lambda *_a: azure_context)
    monkeypatch.setattr(orchestrator, "missing_assignments", lambda *_a: next(missing))
    monkeypatch.setattr(orchestrator, "_require_assignment_write", lambda *_a: None)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)

    def create(_cli, assignment):
        calls.append(assignment)
        return {
            "id": f"{assignment.scope}/providers/Microsoft.Authorization/"
            f"roleAssignments/{assignment.assignment_id}",
        }

    monkeypatch.setattr(orchestrator, "create_assignment", create)
    result = orchestrator.prepare_project(
        config, run_id=run_id, cli=OwnedCli(), refresh_access=True
    )
    assert result["status"] == "access_updated"
    assert calls == [target]
    assert read_json(run_dir / "plan.json")["plan_hash"] == plan["plan_hash"]
    assert not (run_dir / "traffic-receipt.json").exists()
    assert len(result["created_role_assignments"]) == 1
