from __future__ import annotations

import itertools
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from insights_onboarding import agents, cli, orchestrator, provisioning, quality_review, traffic
from insights_onboarding.azure_cli import is_resource_not_found
from insights_onboarding.errors import OnboardingError
from insights_onboarding.insights_api import normalize_run_trigger
from insights_onboarding.models import AgentDeployment, Mutation, OnboardingConfig, OnboardingPlan
from insights_onboarding.permissions import FOUNDRY_USER, RequiredAssignment, required_assignments
from insights_onboarding.receipts import ProvisioningJournal, read_json, write_json_atomic


@pytest.mark.parametrize("mode", ("scratch", "existing"))
def test_bug_bash_policy_never_enables_schedule(make_config, mode):
    config = make_config(mode=mode, profile="bug-bash", create_sample_agent=mode == "existing")
    assert not config.scheduling_enabled
    assert config.creates_sample_agent
    assert config.project_mi_telemetry_required
    orchestrator._validate_agent_selection(config)


def test_one_off_trace_identity_has_no_model_inference_grant(make_resources, azure_context):
    resources = make_resources()
    assignments = required_assignments(
        current_user_id=azure_context.user_object_id,
        project_principal_id=resources.project_principal_id,
        foundry_account_id=resources.foundry_account_resource_id,
        project_id=resources.project_resource_id,
        application_insights_id=resources.application_insights_resource_id,
        workspace_id=resources.log_analytics_workspace_resource_id,
        agent_type="hosted", protected_trace_content=False,
        project_mi_execution=False, project_mi_telemetry=True,
    )
    managed = [item for item in assignments if item.principal_type == "ServicePrincipal"]
    assert len(managed) == 1
    assert managed[0].role.name == "Monitoring Reader"
    assert managed[0].scope == resources.application_insights_resource_id
    assert all(item.scope != resources.foundry_account_resource_id for item in managed)


@pytest.mark.parametrize(
    ("actual", "expected"),
    (("on_demand", "manual"), ("manual", "manual"), ("scheduled", "scheduled"), ("new", "new")),
)
def test_public_on_demand_trigger_is_normalized_without_guessing(actual, expected):
    assert normalize_run_trigger(actual) == expected


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"enable_existing_monitor": True}, "bug_bash_scheduling_unsupported"),
        ({"mode": "existing", "agent_name": "customer"}, "bug_bash_sample_required"),
        ({"invoke_existing_agent": True}, "bug_bash_sample_required"),
    ),
)
def test_bug_bash_conflicting_choices_fail_before_azure(make_config, overrides, code):
    with pytest.raises(OnboardingError) as failure:
        orchestrator._validate_agent_selection(make_config(profile="bug-bash", **overrides))
    assert failure.value.code == code


def test_legacy_config_defaults_are_preserved():
    config = OnboardingConfig(mode="scratch", subscription_id="synthetic")
    assert config.profile == "standard"
    assert config.scheduling_enabled
    assert not OnboardingConfig(mode="existing", subscription_id="synthetic").scheduling_enabled


def test_propagation_failure_keeps_cleanup_receipt(
    monkeypatch, tmp_path, make_config, make_resources, azure_context, run_id
):
    resources = make_resources()
    config = make_config()
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_a, **_k: {"status": "ready"})
    monkeypatch.setattr(orchestrator, "select_context", lambda *_a: azure_context)
    monkeypatch.setattr(orchestrator, "provision_scratch", lambda *_a, **_k: resources)
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_a, **_k: ())

    def fail(**_kwargs):
        raise OnboardingError("role_propagation_timeout", "Synthetic timeout.")

    monkeypatch.setattr(orchestrator, "_wait_for_authorization", fail)
    events = []
    with pytest.raises(OnboardingError, match="Synthetic"):
        orchestrator.onboard(config, run_id=run_id, cli=object(), progress_callback=events.append)
    run_dir = tmp_path / run_id
    assert not (run_dir / "provisioning-receipt.json").exists()
    state = read_json(run_dir / "provisioning-state.json")
    assert state["project"] == asdict(resources)
    assert events[0]["run_dir"] == str(run_dir)

    cleaned = []
    monkeypatch.setattr(
        orchestrator, "cleanup_scratch",
        lambda _cli, **kwargs: cleaned.append(kwargs["resource_group_id"]),
    )
    result = orchestrator.cleanup(run_dir, cli=object())
    assert result["status"] == "complete"
    assert cleaned == [resources.resource_group_id]
    assert orchestrator.cleanup(run_dir, cli=object()) == result
    assert len(cleaned) == 1
    with pytest.raises(OnboardingError) as reused:
        orchestrator.onboard(config, run_id=run_id, cli=object())
    assert reused.value.code == "run_already_cleaned"


def test_journal_preserves_assignments_across_resumed_provisioning(
    tmp_path, make_config, azure_context, run_id
):
    plan = OnboardingPlan.create(
        run_id=run_id, config=make_config(), context=azure_context, mutations=[], expected={}
    ).as_dict()
    journal = ProvisioningJournal(tmp_path, plan)
    assignment = {"id": "assignment", "principal_id": "synthetic"}
    journal.observe("role_assignment", assignment)
    journal.observe("connection", {"id": "connection"})
    resumed = ProvisioningJournal(tmp_path, plan)
    resumed.observe("role_assignment", assignment)
    assert resumed.payload["created_role_assignments"] == [assignment]
    assert resumed.payload["created_connection_ids"] == ["connection"]
    with pytest.raises(OnboardingError) as mismatched:
        ProvisioningJournal(tmp_path, {**plan, "plan_hash": "different"})
    assert mismatched.value.code == "journal_plan_mismatch"


class AdmissionClient:
    def __init__(self, admitted=True):
        self.calls = 0
        self.admitted = admitted

    def get_or_create_monitor(self, **_kwargs):
        return {"id": "monitor", "enabled": False}, True

    def get_monitor(self, monitor_id):
        return {"id": monitor_id, "agent_name": "owned-sample", "enabled": False}

    def list_runs(self, _monitor_id):
        if self.calls and self.admitted:
            return [{"id": "run", "trigger": "manual", "status": "queued"}]
        return []

    def create_run(self, _monitor_id, **_kwargs):
        self.calls += 1
        raise OnboardingError("agent_insights_timeout", "Synthetic lost response.")


def _admit(client, run_dir):
    return orchestrator._admit_quality_run(
        client=client,
        run_dir=run_dir,
        deployment=AgentDeployment("owned-sample", "1", "prompt"),
        model_deployment_name="model",
        lookback_hours=168,
    )


def test_manual_admission_reconciles_without_repeating_post(tmp_path):
    client = AdmissionClient()
    with pytest.raises(OnboardingError):
        _admit(client, tmp_path)
    state = read_json(tmp_path / "insights-state.json")
    assert state["monitor_created"] is True
    assert state["status"] == "admitting"
    resumed = _admit(client, tmp_path)
    assert resumed["run_id"] == "run"
    assert resumed["monitor_created"] is True
    assert _admit(client, tmp_path) == resumed
    assert client.calls == 1


def test_uncertain_admission_never_submits_replacement(tmp_path):
    client = AdmissionClient(admitted=False)
    with pytest.raises(OnboardingError):
        _admit(client, tmp_path)
    with pytest.raises(OnboardingError) as pending:
        _admit(client, tmp_path)
    assert pending.value.code == "insights_admission_unconfirmed"
    assert client.calls == 1


def test_manual_resume_rejects_foreign_version(tmp_path):
    write_json_atomic(
        tmp_path / "insights-state.json",
        {"agent_name": "owned-sample", "agent_version": "other", "run_trigger": "manual"},
    )
    with pytest.raises(OnboardingError) as mismatch:
        _admit(AdmissionClient(), tmp_path)
    assert mismatch.value.code == "insights_state_mismatch"


def test_manual_admission_refuses_changed_live_monitor(tmp_path):
    class ChangedMonitorClient(AdmissionClient):
        def get_monitor(self, monitor_id):
            return {"id": monitor_id, "agent_name": "customer-agent", "enabled": False}

    client = ChangedMonitorClient()
    with pytest.raises(OnboardingError) as changed:
        _admit(client, tmp_path)
    assert changed.value.code == "quality_monitor_mismatch"
    assert client.calls == 0


def test_cleanup_not_found_is_distinct_from_permission_failure():
    assert is_resource_not_found(
        OnboardingError("azure_cli_failed", "Missing.", {"stderr": "ERROR: (ResourceNotFound)"})
    )
    assert not is_resource_not_found(
        OnboardingError("azure_cli_failed", "Denied.", {"stderr": "ERROR: (AuthorizationFailed)"})
    )
    assert not is_resource_not_found(OnboardingError("azure_cli_failed", "Unknown failure."))


def test_synthetic_response_evidence_does_not_persist_reply():
    evidence = {}
    traffic._record_sample_response(
        SimpleNamespace(output_text="The order was delivered successfully."),
        {
            "expected_user_reply": "Report lookup failure.",
            "observed_fault_reply": "The order was delivered successfully.",
        },
        evidence,
    )
    assert evidence == {"response_observed": True, "reply_matches_expected": True}
    traffic._record_sample_response(
        SimpleNamespace(), {"expected_user_reply": "Expected."}, evidence
    )
    assert evidence == {"response_observed": False, "reply_matches_expected": None}


@pytest.mark.parametrize("kind", ("prompt", "hosted"))
@pytest.mark.parametrize("empty", (False, True))
@pytest.mark.parametrize("interrupt_finalization", (False, True))
def test_full_offline_quality_journey_without_azure(
    monkeypatch, tmp_path, make_config, make_resources, azure_context, run_id, capsys,
    kind, empty, interrupt_finalization,
):
    resources = make_resources()
    config = make_config(
        mode="existing", profile="bug-bash", agent_type=kind,
        create_sample_agent=True, project_resource_id=resources.project_resource_id,
        project_endpoint=resources.project_endpoint,
        application_insights_resource_id=resources.application_insights_resource_id,
        model_deployment_name=resources.model_deployment_name,
    )
    deployment = AgentDeployment(
        name=agents.agent_name(run_id, kind), version="1", kind=kind,
        artifact_sha256=quality_review.sample_artifact_digest(kind),
    )
    scenarios = traffic._load_scenarios(kind)
    by_input = {scenario["input"]: scenario for scenario in scenarios}
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    calls = []
    sessions = itertools.count()

    def response(**kwargs):
        if isinstance(kwargs["input"], str):
            scenario = by_input[kwargs["input"]]
            calls.append(scenario["id"])
            if kind == "prompt":
                return SimpleNamespace(
                    id=scenario["id"], status="completed",
                    output=[SimpleNamespace(
                        type="function_call", call_id=f"call-{scenario['id']}",
                        name="lookup_order",
                        arguments=json.dumps(scenario["expected_tool_arguments"]),
                    )],
                )
        else:
            scenario = by_id[kwargs["previous_response_id"]]
        return SimpleNamespace(
            id=f"response-{scenario['id']}", status="completed", output=[],
            output_text=scenario.get("observed_fault_reply", scenario["expected_user_reply"]),
        )

    project = SimpleNamespace(
        get_openai_client=lambda **_kwargs: SimpleNamespace(
            responses=SimpleNamespace(create=response)
        ),
        agents=SimpleNamespace(
            create_session=lambda _name, *, version_indicator: SimpleNamespace(
                agent_session_id=f"session-{next(sessions)}", version_indicator=version_indicator
            ),
            delete_session=lambda *_args: None,
        ),
    )
    monkeypatch.setattr(orchestrator, "_RUNS_ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "doctor", lambda *_a, **_k: {"status": "ready"})
    monkeypatch.setattr(orchestrator, "select_context", lambda *_a: azure_context)
    monkeypatch.setattr(orchestrator, "resolve_existing", lambda *_a, **_k: resources)
    monkeypatch.setattr(
        orchestrator, "list_app_insights_connections", lambda *_a: [{"id": "existing"}]
    )
    monkeypatch.setattr(
        orchestrator, "_existing_caller_capabilities", lambda **_k: (True, True)
    )
    monkeypatch.setattr(orchestrator, "_role_mutations", lambda *_a: [])
    monkeypatch.setattr(orchestrator, "_apply_existing", lambda *_a, **_k: (resources, ()))
    monkeypatch.setattr(orchestrator, "_ensure_roles", lambda *_a, **_k: ())
    monkeypatch.setattr(orchestrator, "_wait_for_authorization", lambda **_k: None)
    monkeypatch.setattr(orchestrator, "_credential", lambda *_a: object())
    monkeypatch.setattr(orchestrator, "project_client", lambda *_a: project)

    def create_agent(*_args, **kwargs):
        assert kwargs["capture_sample_digest"] is True
        kwargs["resource_observer"]("agent", asdict(deployment))
        return deployment

    monkeypatch.setattr(orchestrator, "create_sample_agent", create_agent)
    monkeypatch.setattr(
        orchestrator, "wait_for_ingestion",
        lambda *, outcomes, **_k: [{
            "scenario": item.scenario, "trace_id": f"trace-{item.scenario}",
            "response_id": item.response_id, "session_id": item.session_id, "span_count": 2,
        } for item in outcomes],
    )
    submissions = []
    mutable_service = {"cost": 0, "revision": 1}

    class Insights:
        insights_collection_complete = True

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def probe(self):
            return {"authorized": True}

        def get_or_create_monitor(self, **_kwargs):
            return self.get_monitor("monitor-1"), True

        def get_monitor(self, monitor_id):
            return {
                "id": monitor_id, "agent_name": deployment.name, "enabled": False,
                "estimated_cost": {"amount": mutable_service["cost"], "currency": "USD"},
            }

        def list_runs(self, _monitor_id):
            return (
                [{"id": "insights-run-1", "trigger": "on_demand", "status": "succeeded"}]
                if submissions else []
            )

        def create_run(self, monitor_id, **_kwargs):
            submissions.append(monitor_id)
            return {"id": "insights-run-1"}

        def wait_run(self, **_kwargs):
            return {"id": "insights-run-1", "status": "succeeded"}

        def list_insights(self, _monitor_id, *, include_details):
            assert include_details is True
            if empty:
                return []
            return [{
                "id": f"insight-{mutable_service['revision']}", "run_id": "insights-run-1",
                "agent_name": deployment.name, "agent_version": deployment.version,
                "title": "Synthetic prose-only result for offline workflow validation.",
                "description": "The model reported a problem without a concrete fix.",
            }]

    monkeypatch.setattr(orchestrator, "AgentInsightsClient", Insights)
    run_dir = tmp_path / run_id
    if interrupt_finalization:
        finalize = orchestrator._finalize

        def interrupt(**_kwargs):
            raise RuntimeError("Synthetic interruption after review preparation.")

        monkeypatch.setattr(orchestrator, "_finalize", interrupt)
        with pytest.raises(RuntimeError, match="Synthetic"):
            orchestrator.onboard(config, run_id=run_id, cli=object())
        frozen_monitor = read_json(run_dir / "insights-receipt.json")
        frozen_state = read_json(run_dir / "insights-state.json")
        frozen_input = quality_review.read_review_input(run_dir)
        mutable_service.update(cost=2, revision=2)
        monkeypatch.setattr(orchestrator, "_finalize", finalize)
        final = orchestrator.status(run_dir, cli=object())
        assert read_json(run_dir / "insights-receipt.json") == frozen_monitor
        assert read_json(run_dir / "insights-state.json") == frozen_state
        assert quality_review.read_review_input(run_dir) == frozen_input
    else:
        final = orchestrator.onboard(config, run_id=run_id, cli=object())
    assert final["status"] == "review_pending"
    assert len(calls) == len(set(calls)) == 11
    assert submissions == ["monitor-1"]
    review_input = quality_review.read_review_input(run_dir)
    assert len(review_input["insights"]) == (0 if empty else 1)
    assert quality_review.review_status(run_dir)["human_status"] == "pending"
    assert cli.main(["review", "prepare", "--run-dir", str(run_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["input_digest"] == review_input["input_digest"]

    ai_path = run_dir / "synthetic-ai-input.json"
    write_json_atomic(ai_path, {
        "input_digest": review_input["input_digest"], "overall_assessment": "poor",
        "summary": "Synthetic offline judgment, not an actual model evaluation.",
        "findings": [{
            "insight_id": None if empty else "insight-1",
            "root_cause": "The expected actionable result is missing.",
            "evidence": "The fixed sample traffic is correlated but the result is empty or prose.",
            "fix_assessment": "No concrete fix was provided.",
            "healthy_behavior": "No change was applied to healthy behavior.",
            "uncertainties": ["Synthetic offline test fixture, not live acceptance."],
        }],
    })
    assert cli.main([
        "review", "record-ai", "--run-dir", str(run_dir), "--input", str(ai_path),
    ]) == 0
    capsys.readouterr()
    assert quality_review.review_status(run_dir)["human_status"] == "pending"
    human_path = run_dir / "synthetic-human-input.json"
    write_json_atomic(human_path, {
        "input_digest": review_input["input_digest"], "status": "rated", "rating": 2,
        "comment": "Synthetic test-only response. This is not real participant acceptance.",
    })
    assert cli.main([
        "review", "record-human", "--run-dir", str(run_dir), "--input", str(human_path),
    ]) == 0
    capsys.readouterr()
    state = quality_review.review_status(run_dir)
    assert state["ai_status"] == "recorded"
    assert state["human_status"] == "rated"
    assert state["human_rating"] == 2
    assert state["quality_approved"] is False
    assert read_json(run_dir / "final-receipt.json")["status"] == "complete"
    assert orchestrator.status(run_dir)["status"] == "complete"
    (run_dir / "final-receipt.json").unlink()
    assert orchestrator.status(run_dir, cli=object())["status"] == "complete"
    assert submissions == ["monitor-1"]
    assert len(calls) == 11


def test_new_prompt_captures_digest_before_ownership_observer(monkeypatch, run_id):
    observed = []
    project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=lambda *_a, **_k: [],
        create_version=lambda **_k: SimpleNamespace(version="1"),
    ))
    monkeypatch.setattr(agents, "_prompt_definition", lambda _model: object())
    deployment = agents.create_sample_agent(
        project, run_id=run_id, agent_type="prompt", model="model",
        capture_sample_digest=True,
        resource_observer=lambda kind, value: observed.append((kind, value)),
    )
    assert deployment.artifact_sha256 == quality_review.sample_artifact_digest("prompt")
    assert observed == [
        ("agent_pending", {"name": deployment.name}),
        ("agent", asdict(deployment)),
    ]


def test_hosted_review_digest_matches_uploaded_archive(assets_root, tmp_path):
    actual = agents._deterministic_zip(
        assets_root / "agents" / "hosted-agent", tmp_path / "sample.zip"
    )
    assert actual == quality_review.sample_artifact_digest("hosted")


def test_fresh_quality_run_does_not_adopt_remote_sample(run_id):
    remote = SimpleNamespace(
        version="1", definition=SimpleNamespace(kind="prompt"),
        metadata={
            "agent_insights_quickstart_owner": "agent-insights-quickstart",
            "agent_insights_quickstart_run_id": run_id,
        },
    )
    project = SimpleNamespace(agents=SimpleNamespace(list_versions=lambda *_a, **_k: [remote]))
    events = []
    with pytest.raises(OnboardingError) as existing:
        agents.create_sample_agent(
            project, run_id=run_id, agent_type="prompt", model="model",
            allow_owned_reuse=False,
            resource_observer=lambda kind, value: events.append((kind, value)),
        )
    assert existing.value.code == "agent_creation_not_owned"
    assert events == []


def test_unscoped_insights_are_rejected_after_an_extra_manual_run(tmp_path):
    class ReusedMonitorClient(AdmissionClient):
        def list_runs(self, _monitor_id):
            return [
                {"id": "run", "trigger": "manual", "status": "succeeded"},
                {"id": "later-run", "trigger": "manual", "status": "succeeded"},
            ]

        def wait_run(self, **_kwargs):
            return {"id": "run", "status": "succeeded"}

        def list_insights(self, _monitor_id, **_kwargs):
            return [{"id": "later-insight", "title": "Unscoped result from the later run."}]

    write_json_atomic(tmp_path / "insights-state.json", {
        "status": "started", "agent_name": "owned-sample", "agent_version": "1",
        "monitor_id": "monitor", "run_id": "run", "run_trigger": "manual",
        "monitor_created": True,
    })
    client = ReusedMonitorClient()
    with pytest.raises(OnboardingError) as ambiguous:
        orchestrator._complete_monitor(
            client=client, run_dir=tmp_path,
            deployment=AgentDeployment("owned-sample", "1", "prompt"),
            model_deployment_name="model", enable_monitor=False, lookback_hours=168,
            allow_existing_result=False, timeout_seconds=1, quality_profile=True,
        )
    assert ambiguous.value.code == "quality_run_scope_ambiguous"
    assert client.calls == 0
    assert not (tmp_path / "quality-input.json").exists()
    assert not (tmp_path / "insights-receipt.json").exists()


@pytest.mark.parametrize("resource_kind", ("connection", "role"))
def test_lost_write_response_cannot_be_reported_as_cleaned(
    monkeypatch, tmp_path, make_config, make_resources, azure_context, run_id, resource_kind
):
    resources = make_resources()
    config = make_config(
        mode="existing", profile="bug-bash", create_sample_agent=True,
        project_resource_id=resources.project_resource_id,
        application_insights_resource_id=resources.application_insights_resource_id,
        model_deployment_name=resources.model_deployment_name,
    )
    assignment = RequiredAssignment(
        azure_context.user_object_id, "User", FOUNDRY_USER, resources.project_resource_id
    )
    connection_id = f"{resources.project_resource_id}/connections/planned-connection"
    role_id = (
        f"{resources.project_resource_id}/providers/Microsoft.Authorization/"
        f"roleAssignments/{assignment.assignment_id}"
    )
    mutation = (
        Mutation("create_app_insights_connections", resources.project_resource_id, {
            "project_connection_name": "planned-connection",
        })
        if resource_kind == "connection"
        else Mutation("create_role_assignment", resources.project_resource_id, {
            "principal_id": azure_context.user_object_id,
            "role_definition_id": FOUNDRY_USER.definition_id,
            "assignment_id": assignment.assignment_id,
        })
    )
    plan = OnboardingPlan.create(
        run_id=run_id, config=config, context=azure_context, mutations=[mutation], expected={}
    ).as_dict()
    write_json_atomic(tmp_path / "plan.json", plan)
    journal = ProvisioningJournal(tmp_path, plan)
    journal.observe("project", asdict(resources))

    class LostResponseCli:
        exists = True

        def json(self, *_args, **_kwargs):
            raise OnboardingError("azure_cli_timeout", "Synthetic lost deployment response.")

        def rest(self, *, method, url):
            assert method == "get"
            assert (connection_id if resource_kind == "connection" else role_id) in url
            if not self.exists:
                raise OnboardingError(
                    "azure_cli_failed", "Absent.", {"stderr": "ERROR: (ResourceNotFound)"}
                )
            return {"id": connection_id if resource_kind == "connection" else role_id}

    azure = LostResponseCli()
    if resource_kind == "connection":
        monkeypatch.setattr(provisioning, "list_app_insights_connections", lambda *_a: [])
        monkeypatch.setattr(
            provisioning, "plan_existing_connections",
            lambda *_a, **_k: {"project_connection_name": "planned-connection"},
        )
        with pytest.raises(OnboardingError, match="Synthetic"):
            provisioning.ensure_existing_connections(
                azure, project_resource_id=resources.project_resource_id,
                application_insights_resource_id=resources.application_insights_resource_id,
                location="westus3", run_id=run_id, resource_observer=journal.observe,
            )
    else:
        monkeypatch.setattr(
            orchestrator, "_existing_caller_capabilities", lambda **_k: (False, True)
        )
        monkeypatch.setattr(orchestrator, "missing_assignments", lambda *_a: [assignment])
        monkeypatch.setattr(orchestrator, "create_assignment", lambda *_a: azure.json())
        with pytest.raises(OnboardingError, match="Synthetic"):
            orchestrator._ensure_roles(
                azure, config=config, context=azure_context, resources=resources,
                resource_observer=journal.observe,
            )
    monkeypatch.setattr(orchestrator, "select_context", lambda *_a: azure_context)
    for _ in range(2):
        with pytest.raises(OnboardingError) as uncertain:
            orchestrator.cleanup(tmp_path, cli=azure)
        assert uncertain.value.code == "cleanup_unconfirmed_mutations"
        assert not (tmp_path / "cleanup-receipt.json").exists()
    azure.exists = False
    assert orchestrator.cleanup(tmp_path, cli=azure)["status"] == "complete"
