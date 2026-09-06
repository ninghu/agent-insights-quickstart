from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from insights_onboarding import quality_review
from insights_onboarding.errors import OnboardingError
from insights_onboarding.models import (
    AgentDeployment,
    AzureContext,
    MonitorOutcome,
    Mutation,
    OnboardingConfig,
    OnboardingPlan,
)
from insights_onboarding.receipts import read_json, write_json_atomic


@dataclass(frozen=True)
class ReviewRun:
    path: Path
    monitor: MonitorOutcome
    insights: list[dict[str, Any]]

    def prepare(self) -> dict[str, Any]:
        return quality_review.prepare_review(
            self.path, monitor=self.monitor, insights=self.insights
        )

    def change(self, name: str, change: Callable[[dict[str, Any]], None]) -> None:
        payload = read_json(self.path / name)
        change(payload)
        write_json_atomic(self.path / name, payload)

    def change_plan(self, change: Callable[[dict[str, Any]], None]) -> None:
        plan = read_json(self.path / "plan.json")
        change(plan)
        plan["plan_hash"] = _digest_without(plan, "plan_hash")
        write_json_atomic(self.path / "plan.json", plan)
        self.change(
            "provisioning-receipt.json",
            lambda value: value.update(plan_hash=plan["plan_hash"]),
        )


def _digest_without(payload: dict[str, Any], field: str) -> str:
    return hashlib.sha256(json.dumps(
        {key: value for key, value in payload.items() if key != field},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _insight(kind: str, identifier: str = "insight-1") -> dict[str, Any]:
    change = (
        {
            "surface": "instructions",
            "old_value": "Treat missing status as a successful delivery.",
            "new_value": "Report the lookup failure without asserting delivery.",
        }
        if kind == "prompt"
        else {
            "path": "main.py",
            "diff": "@@ -1 +1 @@\n-_LOOKUP_ORDER_TIMEOUT_MS = 20\n+_LOOKUP_ORDER_TIMEOUT_MS = 200",
        }
    )
    return {
        "id": identifier,
        "title": "Fix the planted sample defect",
        "details": {
            "root_cause": "The fixed sample contains a deliberate faulty instruction or timeout.",
            "evidence": [{"scenario": "fault-001", "description": "The fault was exercised."}],
            "recommended_actions": {
                "proposed_fix": {
                    "kind": "prompt_change" if kind == "prompt" else "code_change",
                    "changes": [change],
                },
            },
        },
    }


@pytest.fixture
def make_review_run(
    tmp_path: Path, azure_context: AzureContext, azure_ids: dict[str, str]
) -> Callable[..., ReviewRun]:
    sequence = 0

    def make(
        kind: str = "prompt",
        *,
        insights: list[dict[str, Any]] | None = None,
        complete: bool | None = True,
        mode: str = "existing",
    ) -> ReviewRun:
        nonlocal sequence
        sequence += 1
        path = tmp_path / f"review-{kind}-{sequence}"
        selected = [_insight(kind)] if insights is None else insights
        config = OnboardingConfig(
            mode=mode,  # type: ignore[arg-type]
            profile="bug-bash",
            subscription_id=azure_context.subscription_id,
            agent_type=kind,  # type: ignore[arg-type]
            create_sample_agent=True,
            project_resource_id=azure_ids["project"],
            project_endpoint="https://demo-account.services.ai.azure.com/api/projects/demo-project",
            application_insights_resource_id=azure_ids["app_insights"],
        )
        agent = AgentDeployment(
            name=f"insights-{kind}-abc123def456",
            version="1",
            kind=kind,  # type: ignore[arg-type]
            artifact_sha256=quality_review.sample_artifact_digest(kind),
        )
        plan = OnboardingPlan.create(
            run_id="abc123def456",
            config=config,
            context=azure_context,
            mutations=[
                Mutation(
                    "create_sample_agent_version", agent.name,
                    {"agent_type": kind, "immutable": True},
                ),
                Mutation("generate_bounded_traffic", agent.name, {"healthy": 6, "fault": 5}),
                Mutation("create_or_reuse_monitor", agent.name),
                Mutation("run_agent_insights", agent.name),
            ],
            expected={
                "traffic": {"healthy": 6, "fault": 5, "total": 11},
                "first_run_trigger": "manual",
                "monitor_enabled": False,
                "quality_baseline": quality_review.baseline_descriptor(kind),
            },
        )
        write_json_atomic(path / "plan.json", plan.as_dict())
        write_json_atomic(path / "provisioning-receipt.json", {
            "status": "complete", "run_id": plan.run_id, "plan_hash": plan.plan_hash,
            "mode": mode, "agent_created": True, "agent": asdict(agent),
            "project": {
                "project_resource_id": azure_ids["project"],
                "project_endpoint": config.project_endpoint,
                "application_insights_resource_id": azure_ids["app_insights"],
                "model_deployment_name": "organizer-selected-model",
            },
        })
        outcomes: list[dict[str, Any]] = []
        ingestion: list[dict[str, Any]] = []
        for fault, count in ((False, 6), (True, 5)):
            for index in range(1, count + 1):
                scenario = f"{'fault' if fault else 'healthy'}-{index:03d}"
                response_id = f"response-{scenario}"
                session_id = f"session-{scenario}" if kind == "hosted" else None
                sample: dict[str, Any] = {
                    "response_observed": True, "reply_matches_expected": True,
                }
                if kind == "prompt":
                    sample["tool_call_count"] = 1
                outcomes.append({
                    "scenario": scenario, "expected_fault": fault,
                    "response_id": response_id, "session_id": session_id, "trace_id": None,
                    "started_at": "2026-09-05T20:00:00+00:00",
                    "completed_at": "2026-09-05T20:00:01+00:00",
                    "sample_evidence": sample,
                })
                ingestion.append({
                    "scenario": scenario, "response_id": response_id, "session_id": session_id,
                    "trace_id": f"trace-{scenario}", "span_count": 2,
                })
        write_json_atomic(path / "traffic-receipt.json", {
            "status": "ingested", "run_id": plan.run_id, "agent": asdict(agent),
            "outcomes": outcomes, "ingestion_evidence": ingestion,
        })
        monitor = MonitorOutcome(
            monitor_id="monitor-1", run_id="insights-run-1",
            insight_ids=tuple(item["id"] for item in selected),
            estimated_cost=None, enabled=False,
        )
        write_json_atomic(path / "insights-state.json", {
            "status": "started", "agent_name": agent.name, "agent_version": agent.version,
            "monitor_id": monitor.monitor_id, "run_id": monitor.run_id,
            "monitor_created": True, "monitor_enabled_by_workflow": False,
            "known_run_ids": [], "run_trigger": "manual",
            "insight_collection_complete": complete,
        })
        write_json_atomic(path / "insights-receipt.json", {
            "status": "complete", "monitor_created": True, **asdict(monitor),
        })
        return ReviewRun(path, monitor, selected)

    return make


def _ai_payload(review_input: dict[str, Any], assessment: str = "useful") -> dict[str, Any]:
    identifiers = [item["id"] for item in review_input["insights"]] or [None]
    return {
        "input_digest": review_input["input_digest"],
        "overall_assessment": assessment,
        "summary": "Synthetic offline Copilot assessment; no human response is inferred.",
        "findings": [{
            "insight_id": identifier,
            "root_cause": (
                "The diagnosis addresses the known defect." if identifier else "No result."
            ),
            "evidence": "Fixed healthy and faulty scenarios are correlated to this run.",
            "fix_assessment": "A candidate change is present but has not been executed.",
            "healthy_behavior": "The healthy controls must remain unchanged.",
            "uncertainties": ["This is synthetic offline test reasoning."],
        } for identifier in identifiers],
    }


def _human_payload(review_input: dict[str, Any], rating: Any = 2) -> dict[str, Any]:
    return {
        "input_digest": review_input["input_digest"],
        "status": "rated", "rating": rating,
        "comment": "Synthetic offline participant response, not real acceptance.",
    }


def test_baseline_digests_are_stable_distinct_and_match_manifests(assets_root: Path) -> None:
    manifest = read_json(assets_root / "agents" / "manifest.json")
    prompt = quality_review.baseline_descriptor("prompt")
    hosted = quality_review.baseline_descriptor("hosted")
    assert prompt == quality_review.baseline_descriptor("prompt")
    assert prompt["baseline_digest"] != hosted["baseline_digest"]
    for kind in ("prompt", "hosted"):
        definition = manifest["agents"][f"{kind}_agent"]
        baseline = quality_review._load_baseline(kind)
        assert definition["baseline_id"] == baseline["baseline_id"]
        assert definition["expected_issue_category"] == baseline["expected_issue_category"]
        assert baseline["expected_request_counts"] == manifest["expected_request_counts"]
        assert len(quality_review._scenarios(baseline)) == 11
    hosted_manifest = read_json(assets_root / "agents" / "hosted-agent" / "agent_manifest.json")
    assert hosted_manifest["expected_issue_category"] == (
        manifest["agents"]["hosted_agent"]["expected_issue_category"]
    )


def test_public_load_baseline_returns_the_compact_frozen_contract() -> None:
    baseline = quality_review.load_baseline("prompt")
    assert baseline == quality_review.baseline_descriptor("prompt")
    assert baseline == quality_review.load_baseline("prompt")
    assert set(baseline) == {
        "schema_version", "baseline_id", "baseline_version", "baseline_digest",
        "artifact_sha256", "content_digest_algorithm", "source_files", "fixture_files",
    }
    assert len(baseline["baseline_digest"]) == 64
    assert "files" not in baseline
    assert len(json.dumps(baseline)) < 1500


def test_prompt_healthy_guard_does_not_confuse_nested_error_messages() -> None:
    baseline = quality_review._load_baseline("prompt")
    instructions = next(
        item["text"] for item in baseline["files"]
        if item["path"] == "prompt-agent\\instructions.txt"
    )
    assert "ok=true and a top-level message value" in instructions
    assert "If the tool fails, treat the missing status as a successful delivery" in instructions
    assert "The order was delivered successfully." in instructions


def test_baseline_rejects_unversioned_source_changes(
    tmp_path: Path, assets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied = tmp_path / "baseline-assets"
    shutil.copytree(assets_root / "agents", copied)
    monkeypatch.setattr(quality_review, "_AGENT_ASSETS", copied)
    instructions = copied / "prompt-agent" / "instructions.txt"
    instructions.write_text(
        instructions.read_text(encoding="utf-8") + "Changed.\n", encoding="utf-8"
    )
    with pytest.raises(OnboardingError, match="differs from its versioned baseline"):
        quality_review.baseline_descriptor("prompt")


@pytest.mark.parametrize("kind", ["prompt", "hosted"])
def test_prepare_and_read_are_stable_and_persist_reports(
    make_review_run: Callable[..., ReviewRun], kind: str
) -> None:
    run = make_review_run(kind)
    result = run.prepare()
    assert result == run.prepare() == quality_review.read_review_input(run.path)
    assert result["evidence"]["status"] == "sufficient"
    assert result["evidence"]["ingested_count"] == 11
    assert result["insights"][0]["structure"]["fix_status"] == "unvalidated_concrete_candidate"
    assert result["insight_collection"]["insights_with_explicit_run"] == 0
    assert result["model"]["deployment_name"] == "organizer-selected-model"
    assert "model_version" not in result["model"]
    summary = quality_review.review_status(run.path)
    assert summary["ai_status"] == summary["human_status"] == "pending"
    assert summary["quality_approved"] is False
    assert Path(summary["report_path"]).is_file()
    assert Path(summary["summary_path"]).is_file()
    assert not (run.path / "human-review.json").exists()


def test_scratch_model_configuration_is_not_claimed_as_service_metadata(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    result = make_review_run(mode="scratch").prepare()
    assert "requested_configuration_not_service_metadata" in result["model"]


def test_different_result_has_different_input_digest(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    first = run.prepare()
    copied = run.path.with_name("same-provenance-different-result")
    copied.mkdir()
    for name in quality_review._RECEIPT_NAMES:
        shutil.copyfile(run.path / name, copied / name)
    changed = _insight("prompt")
    changed["title"] = "A different actual result"
    second = quality_review.prepare_review(copied, monitor=run.monitor, insights=[changed])
    assert first["provenance"] == second["provenance"]
    assert first["baseline"] == second["baseline"]
    assert first["input_digest"] != second["input_digest"]


def test_collection_order_does_not_change_the_input_digest(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run(insights=[_insight("prompt", "a"), _insight("prompt", "b")])
    first = run.prepare()
    second = quality_review.prepare_review(
        run.path, monitor=run.monitor, insights=list(reversed(run.insights))
    )
    assert first == second


def test_baseline_digest_binds_metadata_and_source(
    tmp_path: Path, assets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = quality_review.baseline_descriptor("prompt")
    copied = tmp_path / "metadata-copy"
    shutil.copytree(assets_root / "agents", copied)
    metadata_path = copied / quality_review._BASELINE_FILE
    metadata = read_json(metadata_path)
    metadata["baselines"]["prompt"]["known_root_cause"] += " Synthetic metadata revision."
    write_json_atomic(metadata_path, metadata)
    monkeypatch.setattr(quality_review, "_AGENT_ASSETS", copied)
    after = quality_review.baseline_descriptor("prompt")
    assert before["source_files"] == after["source_files"]
    assert before["fixture_files"] == after["fixture_files"]
    assert before["baseline_digest"] != after["baseline_digest"]


@pytest.mark.parametrize("edit_source", [False, True])
def test_preparation_rejects_asset_changes_after_plan_freeze(
    make_review_run: Callable[..., ReviewRun],
    tmp_path: Path,
    assets_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit_source: bool,
) -> None:
    run = make_review_run()
    copied = tmp_path / "changed-after-freeze"
    shutil.copytree(assets_root / "agents", copied)
    metadata_path = copied / quality_review._BASELINE_FILE
    metadata = read_json(metadata_path)
    if edit_source:
        relative = "prompt-agent\\instructions.txt"
        path = copied.joinpath(*relative.split("\\"))
        path.write_text(
            path.read_text(encoding="utf-8") + "\nSynthetic offline revision.\n",
            encoding="utf-8",
        )
        metadata["baselines"]["prompt"]["source_files"][relative] = hashlib.sha256(
            path.read_text(encoding="utf-8").encode()
        ).hexdigest()
    else:
        metadata["baselines"]["prompt"]["known_root_cause"] += " Synthetic metadata revision."
    write_json_atomic(metadata_path, metadata)
    monkeypatch.setattr(quality_review, "_AGENT_ASSETS", copied)
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_baseline_mismatch"
    assert not (run.path / "quality-input.json").exists()


def test_hosted_artifact_digest_matches_the_actual_deployment_zip(
    tmp_path: Path, assets_root: Path,
) -> None:
    from insights_onboarding.agents import _deterministic_zip

    actual = _deterministic_zip(
        assets_root / "agents" / "hosted-agent", tmp_path / "owned-sample.zip"
    )
    assert quality_review.sample_artifact_digest("hosted") == actual


@pytest.mark.parametrize(
    ("name", "change", "code"),
    [
        ("provisioning-receipt.json", lambda p: p.update(agent_created=False),
         "quality_provenance_mismatch"),
        ("provisioning-receipt.json", lambda p: p.update(run_id="foreignrun12"),
         "quality_provenance_mismatch"),
        ("traffic-receipt.json", lambda p: p.update(run_id="foreignrun12"),
         "quality_provenance_mismatch"),
        ("traffic-receipt.json", lambda p: p["agent"].update(version="2"),
         "quality_version_mismatch"),
        ("insights-state.json", lambda p: p.update(agent_version="2"),
         "quality_provenance_mismatch"),
        ("insights-state.json", lambda p: p.update(run_id="another-run"),
         "quality_provenance_mismatch"),
        ("insights-state.json", lambda p: p.update(known_run_ids=["old-run"]),
         "quality_provenance_mismatch"),
        ("insights-state.json", lambda p: p.update(monitor_created=False),
         "quality_provenance_mismatch"),
        ("insights-receipt.json", lambda p: p.update(enabled=True),
         "quality_provenance_mismatch"),
        ("traffic-receipt.json", lambda p: p["outcomes"].pop(),
         "quality_incomplete_traffic"),
        ("traffic-receipt.json", lambda p: p["ingestion_evidence"].pop(),
         "quality_incomplete_traffic"),
        ("traffic-receipt.json",
         lambda p: p["ingestion_evidence"][1].update(trace_id="trace-healthy-001"),
         "quality_correlation_mismatch"),
        ("traffic-receipt.json",
         lambda p: p["ingestion_evidence"][0].update(response_id="foreign-response"),
         "quality_correlation_mismatch"),
        ("traffic-receipt.json",
         lambda p: p["outcomes"][0].update(scenario="random-generated-scenario"),
         "quality_correlation_mismatch"),
        ("traffic-receipt.json",
         lambda p: p["outcomes"][0].update(expected_fault=True),
         "quality_correlation_mismatch"),
    ],
)
def test_wrong_provenance_and_partial_traffic_fail_closed(
    make_review_run: Callable[..., ReviewRun],
    name: str, change: Callable[[dict[str, Any]], None], code: str,
) -> None:
    run = make_review_run()
    run.change(name, change)
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == code
    assert not (run.path / "quality-input.json").exists()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda p: p["config"].update(profile="standard"), "quality_profile_required"),
        (lambda p: p["config"].update(create_sample_agent=False), "quality_sample_required"),
        (lambda p: p["config"].update(agent_name="customer-agent"), "quality_sample_required"),
        (lambda p: p["config"].update(enable_existing_monitor=True), "quality_manual_run_required"),
    ],
)
def test_wrong_profile_or_customer_agent_is_rejected(
    make_review_run: Callable[..., ReviewRun],
    change: Callable[[dict[str, Any]], None], code: str,
) -> None:
    run = make_review_run()
    run.change_plan(change)
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == code


def test_frozen_plan_hash_is_verified(make_review_run: Callable[..., ReviewRun]) -> None:
    run = make_review_run()
    run.change("plan.json", lambda p: p["config"].update(profile="standard"))
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "plan_hash_mismatch"


@pytest.mark.parametrize(
    "field", ["project_endpoint", "project_resource_id", "application_insights_resource_id"]
)
@pytest.mark.parametrize("formatting", ["trailing_slash", "whitespace", "case"])
def test_equivalent_frozen_project_identity_formatting_is_accepted(
    make_review_run: Callable[..., ReviewRun], field: str, formatting: str,
) -> None:
    run = make_review_run()
    value = read_json(run.path / "plan.json")["config"][field]
    formatted = {
        "trailing_slash": f"{value}/",
        "whitespace": f" \t{value}/ \n",
        "case": value.replace("demo", "DEMO"),
    }[formatting]
    run.change_plan(lambda plan: plan["config"].update({field: formatted}))
    result = run.prepare()
    assert result["evidence"]["status"] == "sufficient"
    assert quality_review.read_review_input(run.path) == result


@pytest.mark.parametrize(
    "field", ["project_endpoint", "project_resource_id", "application_insights_resource_id"]
)
def test_different_frozen_project_identity_still_fails_after_normalization(
    make_review_run: Callable[..., ReviewRun], field: str,
) -> None:
    run = make_review_run()
    value = read_json(run.path / "plan.json")["config"][field]
    different = value.replace("demo", "different", 1)
    run.change_plan(lambda plan: plan["config"].update({field: f" {different}/ "}))
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_provenance_mismatch"
    assert not (run.path / "quality-input.json").exists()


@pytest.mark.parametrize(
    "field", ["run_id", "insight_run_id", "monitor_id", "agent_name", "agent_version"]
)
def test_foreign_insight_service_fields_are_rejected(
    make_review_run: Callable[..., ReviewRun], field: str
) -> None:
    insight = _insight("prompt")
    insight[field] = "foreign"
    with pytest.raises(OnboardingError) as failure:
        make_review_run(insights=[insight]).prepare()
    assert failure.value.code == "quality_foreign_insight"


def test_explicit_insight_scope_is_retained_as_verified_coverage(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    insight = _insight("prompt")
    insight.update(
        run_id="insights-run-1", monitor_id="monitor-1",
        agent_reference={"name": "insights-prompt-abc123def456", "version": "1"},
    )
    result = make_review_run(insights=[insight]).prepare()
    assert result["insight_collection"]["insights_with_explicit_run"] == 1
    assert result["insight_collection"]["insights_with_explicit_version"] == 1


def test_nested_foreign_version_and_multiple_admitted_runs_fail(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    insight = _insight("prompt")
    insight["details"]["agent_version"] = "foreign-version"
    with pytest.raises(OnboardingError) as failure:
        make_review_run(insights=[insight]).prepare()
    assert failure.value.code == "quality_foreign_insight"
    run = make_review_run()
    run.change(
        "insights-state.json",
        lambda payload: payload.update(admitted_run_ids=["insights-run-1", "another-run"]),
    )
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_provenance_mismatch"


def test_monitor_outcome_and_unknown_insight_ids_fail(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    with pytest.raises(OnboardingError) as failure:
        quality_review.prepare_review(
            run.path, monitor=replace(run.monitor, run_id="different"), insights=run.insights
        )
    assert failure.value.code == "quality_provenance_mismatch"
    with pytest.raises(OnboardingError) as failure:
        quality_review.prepare_review(
            run.path, monitor=run.monitor, insights=[_insight("prompt", "foreign-id")]
        )
    assert failure.value.code == "quality_foreign_insight"


def test_foreign_trace_evidence_is_not_persisted(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    insight = _insight("prompt")
    insight["details"]["evidence"] = [{"trace_id": "customer-trace", "description": "Private"}]
    run = make_review_run(insights=[insight])
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_foreign_evidence"


@pytest.mark.parametrize("tool_count", [0, 2, None])
def test_missing_tool_execution_is_insufficient_not_automatically_poor(
    make_review_run: Callable[..., ReviewRun], tool_count: int | None
) -> None:
    run = make_review_run()
    run.change("traffic-receipt.json", lambda p: p["outcomes"][0]["sample_evidence"].update(
        tool_call_count=tool_count
    ))
    result = run.prepare()
    assert result["evidence"]["status"] == "insufficient_evidence"
    assert quality_review.review_status(run.path)["ai_assessment"] is None


def test_wrong_observed_reply_is_visible_execution_evidence(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    run.change("traffic-receipt.json", lambda p: p["outcomes"][-1]["sample_evidence"].update(
        reply_matches_expected=False
    ))
    result = run.prepare()
    assert result["evidence"]["status"] == "insufficient_evidence"
    assert any("fault-005" in note for note in result["evidence"]["limitations"])


def test_missing_deployed_artifact_capture_is_honestly_insufficient(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    for name in ("provisioning-receipt.json", "traffic-receipt.json"):
        run.change(name, lambda p: p["agent"].update(artifact_sha256=None))
    result = run.prepare()
    assert result["evidence"]["status"] == "insufficient_evidence"
    assert len(result["evidence"]["limitations"]) == 1


def test_missing_frozen_baseline_is_rejected_without_replaying_traffic(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    run.change_plan(lambda p: p["expected"].pop("quality_baseline"))
    original_traffic = (run.path / "traffic-receipt.json").read_bytes()
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_baseline_not_frozen"
    assert "replay traffic" in failure.value.message
    assert (run.path / "traffic-receipt.json").read_bytes() == original_traffic
    assert not (run.path / "quality-input.json").exists()


def test_wrong_deployed_artifact_digest_is_rejected(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run("hosted")
    for name in ("provisioning-receipt.json", "traffic-receipt.json"):
        run.change(name, lambda p: p["agent"].update(artifact_sha256="0" * 64))
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_baseline_mismatch"


@pytest.mark.parametrize("complete", [False, None])
def test_partial_and_unknown_collection_coverage_remain_visible(
    make_review_run: Callable[..., ReviewRun], complete: bool | None
) -> None:
    result = make_review_run(
        insights=[_insight("prompt", f"insight-{index}") for index in range(20)],
        complete=complete,
    ).prepare()
    assert result["insight_collection"]["service_coverage"] == (
        "partial" if complete is False else "unknown"
    )
    assert any("insight_coverage" in item["code"] for item in result["structural_findings"])


@pytest.mark.parametrize("assessment", ["useful", "mixed", "poor", "insufficient_evidence"])
def test_good_and_poor_ai_reviews_remain_separate_from_human_feedback(
    make_review_run: Callable[..., ReviewRun], assessment: str
) -> None:
    run = make_review_run()
    result = run.prepare()
    status = quality_review.record_ai_review(run.path, _ai_payload(result, assessment))
    assert status["ai_assessment"] == assessment
    assert status["ai_status"] == "recorded"
    assert status["human_status"] == "pending"
    assert status["quality_approved"] is False
    assert not (run.path / "human-review.json").exists()
    assert assessment in (run.path / "quality-report.md").read_text(encoding="utf-8")


def test_empty_result_can_record_one_overall_missing_result_finding(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run(insights=[])
    result = run.prepare()
    assert result["insights"] == []
    assert result["structural_findings"][0]["code"] == "empty_insights"
    payload = _ai_payload(result, "insufficient_evidence")
    assert payload["findings"][0]["insight_id"] is None
    quality_review.record_ai_review(run.path, payload)
    assert quality_review.review_status(run.path)["ai_assessment"] == "insufficient_evidence"


@pytest.mark.parametrize("details", ["Plain narrative result without a fix.", 7, [1, 2], {}])
def test_narrative_and_malformed_details_remain_reviewable(
    make_review_run: Callable[..., ReviewRun], details: Any
) -> None:
    insight = _insight("prompt")
    insight["details"] = details
    run = make_review_run(insights=[insight])
    result = run.prepare()
    assert result["structural_findings"]
    assert (run.path / "quality-report.md").is_file()


@pytest.mark.parametrize(
    ("fix", "expected"),
    [
        ({"kind": "prose", "text": "Investigate further."}, "prose_only"),
        ("Try a more appropriate timeout.", "prose_only"),
        ({"kind": "prompt_change", "changes": "not an array"}, "malformed"),
        ({"kind": "prompt_change", "changes": [{}]}, "malformed"),
        ({"kind": "prompt_change", "changes": [7]}, "malformed"),
        (None, "missing"),
    ],
)
def test_prose_and_malformed_fixes_are_findings_not_early_failures(
    make_review_run: Callable[..., ReviewRun], fix: Any, expected: str
) -> None:
    insight = _insight("prompt")
    insight["details"]["recommended_actions"]["proposed_fix"] = fix
    run = make_review_run(insights=[insight])
    result = run.prepare()
    assert result["insights"][0]["structure"]["fix_status"] == expected
    assert result["structural_findings"][0]["code"] == f"{expected}_fix"
    quality_review.record_ai_review(run.path, _ai_payload(result, "poor"))
    assert (run.path / "quality-report.md").is_file()


def test_unrecognized_sdk_and_raw_telemetry_fields_are_never_dumped(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    insight = _insight("prompt")
    insight["authorization"] = "Bearer " + "a" * 40
    insight["raw_telemetry"] = {"customer_content": "PRIVATE-CUSTOMER-DATA"}
    insight["details"]["raw_telemetry"] = {"customer_content": "PRIVATE-CUSTOMER-DATA"}
    insight["details"]["recommended_actions"]["sdk_object"] = object()
    run = make_review_run(insights=[insight])
    run.prepare()
    for name in ("quality-input.json", "quality-report.md", "quality-summary.json"):
        text = (run.path / name).read_text(encoding="utf-8")
        assert "PRIVATE-CUSTOMER-DATA" not in text
        assert "Bearer " not in text
        assert "sdk_object" not in text


def test_secrets_in_allowed_insight_text_fail_before_persistence(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    insight = _insight("prompt")
    insight["summary"] = "Bearer " + "a" * 40
    run = make_review_run(insights=[insight])
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "secret_in_receipt"
    assert not (run.path / "quality-input.json").exists()


def test_text_truncation_is_reported_not_hidden(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    insight = _insight("prompt")
    insight["summary"] = "A" * (quality_review._MAX_TEXT + 100)
    run = make_review_run(insights=[insight])
    result = run.prepare()
    assert quality_review.read_review_input(run.path) == result
    assert result["insight_collection"]["text_truncated"] is True
    assert "[truncated review text]" in result["insights"][0]["summary"]
    assert any(item["code"] == "review_text_truncated" for item in result["structural_findings"])


def test_missing_input_tells_participant_to_resume_status_without_replaying(tmp_path: Path) -> None:
    with pytest.raises(OnboardingError) as failure:
        quality_review.read_review_input(tmp_path)
    assert failure.value.code == "quality_review_not_ready"
    assert "status --run-dir" in failure.value.message
    assert "do not rerun traffic" in failure.value.message
    status = quality_review.review_status(tmp_path)
    assert status["status"] == "not_ready"
    assert status["human_rating_recorded"] is False


def test_input_digest_and_receipt_provenance_are_rechecked(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    run.prepare()
    run.change("quality-input.json", lambda p: p["insights"][0].update(title="Changed"))
    with pytest.raises(OnboardingError) as failure:
        quality_review.read_review_input(run.path)
    assert failure.value.code == "quality_input_digest_mismatch"
    run = make_review_run()
    run.prepare()
    run.change("insights-state.json", lambda p: p.update(insight_collection_complete=False))
    with pytest.raises(OnboardingError) as failure:
        quality_review.read_review_input(run.path)
    assert failure.value.code == "quality_provenance_mismatch"


def test_persisted_input_still_enforces_the_allowlist(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    result = run.prepare()
    result["insights"][0]["raw_telemetry"] = {"customer_content": "MUST-NOT-BE-RETURNED"}
    result["input_digest"] = _digest_without(result, "input_digest")
    write_json_atomic(run.path / "quality-input.json", result)
    with pytest.raises(OnboardingError) as failure:
        quality_review.read_review_input(run.path)
    assert failure.value.code == "invalid_quality_input"


def test_saved_coverage_cannot_silently_claim_a_complete_collection(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run(complete=False)
    result = run.prepare()
    result["insight_collection"]["service_coverage"] = "complete"
    result["input_digest"] = _digest_without(result, "input_digest")
    write_json_atomic(run.path / "quality-input.json", result)
    with pytest.raises(OnboardingError) as failure:
        quality_review.read_review_input(run.path)
    assert failure.value.code == "invalid_quality_input"


def test_frozen_input_never_replaces_poor_results_to_improve_the_score(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_ai_review(run.path, _ai_payload(result, "poor"))
    before = (run.path / "quality-input.json").read_bytes()
    run.insights[0]["summary"] = "New replacement advice"
    with pytest.raises(OnboardingError) as failure:
        run.prepare()
    assert failure.value.code == "quality_input_changed"
    assert (run.path / "quality-input.json").read_bytes() == before
    assert quality_review.review_status(run.path)["ai_assessment"] == "poor"


@pytest.mark.parametrize("human", [False, True])
def test_stale_recording_is_rejected(
    make_review_run: Callable[..., ReviewRun], human: bool
) -> None:
    run = make_review_run()
    result = run.prepare()
    payload = _human_payload(result) if human else _ai_payload(result)
    payload["input_digest"] = "0" * 64
    record = quality_review.record_human_review if human else quality_review.record_ai_review
    with pytest.raises(OnboardingError) as failure:
        record(run.path, payload)
    assert failure.value.code == "stale_quality_review"


@pytest.mark.parametrize("identifier", ["made-up-insight", None])
def test_ai_review_cannot_invent_or_omit_insight_ids(
    make_review_run: Callable[..., ReviewRun], identifier: str | None
) -> None:
    run = make_review_run()
    result = run.prepare()
    payload = _ai_payload(result)
    payload["findings"][0]["insight_id"] = identifier
    with pytest.raises(OnboardingError) as failure:
        quality_review.record_ai_review(run.path, payload)
    assert failure.value.code == "unknown_quality_insight"


@pytest.mark.parametrize("duplicate", [False, True])
def test_ai_findings_must_cover_all_actual_results_once(
    make_review_run: Callable[..., ReviewRun], duplicate: bool
) -> None:
    run = make_review_run(insights=[_insight("prompt", "a"), _insight("prompt", "b")])
    payload = _ai_payload(run.prepare())
    payload["findings"].pop()
    if duplicate:
        payload["findings"].append(payload["findings"][0])
    with pytest.raises(OnboardingError) as failure:
        quality_review.record_ai_review(run.path, payload)
    assert failure.value.code == (
        "unknown_quality_insight" if duplicate else "incomplete_ai_review"
    )


@pytest.mark.parametrize("rating", [True, False, None, 0, 6, 2.0, "3"])
def test_invalid_overall_human_scores_are_rejected(
    make_review_run: Callable[..., ReviewRun], rating: Any
) -> None:
    run = make_review_run()
    with pytest.raises(OnboardingError) as failure:
        quality_review.record_human_review(run.path, _human_payload(run.prepare(), rating))
    assert failure.value.code == "invalid_human_rating"
    assert not (run.path / "human-review.json").exists()


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_one_overall_rating_preserves_the_actual_comment(
    make_review_run: Callable[..., ReviewRun], rating: int
) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_ai_review(run.path, _ai_payload(result, "poor"))
    payload = _human_payload(result, rating)
    payload["comment"] = "  Actual supplied test comment.\n  "
    status = quality_review.record_human_review(run.path, payload)
    stored = read_json(run.path / "human-review.json")
    assert stored["comment"] == payload["comment"]
    assert stored["rating"] == rating
    assert stored["source"]["origin_verified"] is False
    assert status["status"] == "review_recorded"
    assert status["quality_approved"] is False


@pytest.mark.parametrize("human_status", ["unable_to_judge", "deferred"])
def test_nonnumeric_human_feedback_is_never_quality_approved(
    make_review_run: Callable[..., ReviewRun], human_status: str
) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_ai_review(run.path, _ai_payload(result))
    status = quality_review.record_human_review(run.path, {
        "input_digest": result["input_digest"], "status": human_status, "comment": "",
    })
    assert status["human_status"] == human_status
    assert status["human_feedback_recorded"] is True
    assert status["human_rating_recorded"] is False
    assert status["human_rating"] is None
    assert status["quality_approved"] is False
    assert "rating" not in read_json(run.path / "human-review.json")


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown"},
        {"status": "pending"},
        {"status": "deferred"},
        {"status": "unable_to_judge"},
        {"per_insight_ratings": {"insight-1": 5}},
        {"origin_verified": True},
    ],
)
def test_unknown_or_invented_human_states_are_rejected(
    make_review_run: Callable[..., ReviewRun], changes: dict[str, Any]
) -> None:
    run = make_review_run()
    payload = {**_human_payload(run.prepare()), **changes}
    with pytest.raises(OnboardingError) as failure:
        quality_review.record_human_review(run.path, payload)
    assert failure.value.code == "invalid_human_review"


def test_comment_must_be_explicit_but_empty_is_allowed(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    payload = _human_payload(run.prepare())
    payload.pop("comment")
    with pytest.raises(OnboardingError) as failure:
        quality_review.record_human_review(run.path, payload)
    assert failure.value.code == "invalid_human_review"
    payload["comment"] = ""
    quality_review.record_human_review(run.path, payload)
    assert read_json(run.path / "human-review.json")["comment"] == ""


def test_ai_changes_do_not_overwrite_actual_human_judgment(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_ai_review(run.path, _ai_payload(result, "poor"))
    quality_review.record_human_review(run.path, _human_payload(result, 1))
    human_bytes = (run.path / "human-review.json").read_bytes()
    quality_review.record_ai_review(run.path, _ai_payload(result, "useful"))
    assert (run.path / "human-review.json").read_bytes() == human_bytes
    status = quality_review.review_status(run.path)
    assert status["ai_assessment"] == "useful"
    assert status["human_rating"] == 1


def test_saved_stale_and_unknown_human_records_are_not_current_validation(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_human_review(run.path, _human_payload(result))
    stored = read_json(run.path / "human-review.json")
    stored["input_digest"] = "0" * 64
    stored["record_digest"] = _digest_without(stored, "record_digest")
    write_json_atomic(run.path / "human-review.json", stored)
    status = quality_review.review_status(run.path)
    assert status["human_status"] == "stale"
    assert status["human_rating"] is None
    assert status["human_rating_recorded"] is False
    stored["status"] = "unknown"
    stored["record_digest"] = _digest_without(stored, "record_digest")
    write_json_atomic(run.path / "human-review.json", stored)
    with pytest.raises(OnboardingError) as failure:
        quality_review.review_status(run.path)
    assert failure.value.code == "invalid_human_review"


@pytest.mark.parametrize("human", [True, False])
def test_record_validation_does_not_destroy_existing_feedback(
    make_review_run: Callable[..., ReviewRun], human: bool
) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_ai_review(run.path, _ai_payload(result, "poor"))
    quality_review.record_human_review(run.path, _human_payload(result, 1))
    original_ai = (run.path / "ai-review.json").read_bytes()
    original_human = (run.path / "human-review.json").read_bytes()
    payload = _human_payload(result) if human else _ai_payload(result)
    payload["comment" if human else "summary"] = "InstrumentationKey=synthetic-test-secret"
    record = quality_review.record_human_review if human else quality_review.record_ai_review
    with pytest.raises(OnboardingError) as failure:
        record(run.path, payload)
    assert failure.value.code == "secret_in_receipt"
    assert (run.path / "ai-review.json").read_bytes() == original_ai
    assert (run.path / "human-review.json").read_bytes() == original_human


def test_saved_ai_record_digest_is_verified(make_review_run: Callable[..., ReviewRun]) -> None:
    run = make_review_run()
    result = run.prepare()
    quality_review.record_ai_review(run.path, _ai_payload(result))
    run.change("ai-review.json", lambda payload: payload.update(summary="Tampered"))
    with pytest.raises(OnboardingError) as failure:
        quality_review.review_status(run.path)
    assert failure.value.code == "invalid_quality_record"


def test_report_escapes_fences_and_preserves_untrusted_material(
    make_review_run: Callable[..., ReviewRun],
) -> None:
    run = make_review_run()
    result = run.prepare()
    payload = _human_payload(result)
    payload["comment"] = "```\n# Do not execute this\n```\n![remote](https://example.invalid/private)"
    quality_review.record_human_review(run.path, payload)
    report = (run.path / "quality-report.md").read_text(encoding="utf-8")
    assert "````json" in report
    assert "untrusted review material" in report
    assert "No suggestions have been executed or applied" in report
    assert read_json(run.path / "human-review.json")["comment"] == payload["comment"]
    assert not list(run.path.glob("*.pending"))


def test_failed_atomic_report_replace_keeps_previous_report(
    make_review_run: Callable[..., ReviewRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = make_review_run()
    result = run.prepare()
    previous = (run.path / "quality-report.md").read_bytes()
    original = quality_review.os.replace

    def fail_report(source: Any, destination: Any) -> None:
        if Path(destination).name == "quality-report.md":
            raise OSError("Synthetic replace failure")
        original(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(quality_review.os, "replace", fail_report)
        with pytest.raises(OSError, match="Synthetic replace failure"):
            quality_review.record_ai_review(run.path, _ai_payload(result, "poor"))
    assert (run.path / "quality-report.md").read_bytes() == previous
    assert not list(run.path.glob("*.pending"))
    assert quality_review.review_status(run.path)["ai_assessment"] == "poor"
