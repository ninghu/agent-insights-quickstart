"""Opt-in, disposable, manual-only quality smoke tests; never human acceptance."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import re
import secrets
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from . import orchestrator
from .azure_cli import AzureCli
from .discovery import select_context
from .errors import OnboardingError
from .models import AgentType, AzureContext, OnboardingConfig, ProjectResources
from .provisioning import cleanup_scratch, provision_scratch, resource_group_name
from .receipts import ResourceObserver, ensure_secret_free, write_json_atomic
from .validation import require_owned_tags, validate_run_id

MATRIX_SCHEMA_VERSION = 2
MANIFEST_SCHEMA_VERSION = 1
PROFILE: Literal["bug-bash"] = "bug-bash"
_SKILL_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SKILL_ROOT.parents[2]
_GROUP_PREFIXES = ("rg-insights-live-fixture-", "rg-insights-live-matrix-")
_EXECUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class LiveMatrixCase:
    name: str
    mode: Literal["scratch", "existing"]
    agent_type: AgentType


SUPPORTED_CASES = (
    LiveMatrixCase("existing-create-prompt-oneoff", "existing", "prompt"),
    LiveMatrixCase("existing-create-hosted-oneoff", "existing", "hosted"),
)
FALLBACK_CASES = (
    LiveMatrixCase("scratch-prompt-oneoff", "scratch", "prompt"),
    LiveMatrixCase("scratch-hosted-oneoff", "scratch", "hosted"),
)
CASES_BY_NAME = {case.name: case for case in (*SUPPORTED_CASES, *FALLBACK_CASES)}


@dataclass(frozen=True, slots=True)
class LiveMatrixOptions:
    subscription_id: str
    location: str
    model_name: str
    model_version: str
    model_format: str
    model_sku: str
    model_capacity: int
    cases: tuple[LiveMatrixCase, ...]
    output_dir: Path
    ingestion_timeout_seconds: float
    insights_timeout_seconds: float
    resume_summary: Path | None = None
    execution_id: str | None = None


@dataclass(slots=True)
class FixtureProject:
    run_id: str
    config: OnboardingConfig
    context: AzureContext
    resources: ProjectResources


def supported_case_names() -> tuple[str, ...]:
    return tuple(case.name for case in SUPPORTED_CASES)


def select_cases(value: str) -> tuple[LiveMatrixCase, ...]:
    selected = [item.strip() for item in value.split(",") if item.strip()]
    if not selected or selected == ["all"]:
        return SUPPORTED_CASES
    unknown = sorted(set(selected) - set(CASES_BY_NAME))
    if unknown:
        raise OnboardingError(
            "unknown_live_matrix_case",
            "'all' selects only the two existing-project sample cases; "
            "scratch fallback cases require their explicit names.",
            {"unknown": unknown, "available": list(CASES_BY_NAME)},
        )
    return tuple(case for name, case in CASES_BY_NAME.items() if name in selected)


def case_config(
    case: LiveMatrixCase,
    options: LiveMatrixOptions,
    *,
    fixture: FixtureProject | None = None,
) -> OnboardingConfig:
    if case not in CASES_BY_NAME.values():
        raise OnboardingError("unknown_live_matrix_case", "Unsupported bug-bash case.")
    if case.mode == "scratch":
        return OnboardingConfig(
            mode="scratch",
            profile=PROFILE,
            subscription_id=options.subscription_id,
            location=options.location,
            agent_type=case.agent_type,
            name_prefix="insights-live-matrix",
            model_name=options.model_name,
            model_version=options.model_version,
            model_format=options.model_format,
            model_sku=options.model_sku,
            model_capacity=options.model_capacity,
        )
    if fixture is None:
        raise OnboardingError(
            "live_matrix_fixture_missing",
            "Existing-project cases require the disposable prepared fixture.",
        )
    return OnboardingConfig(
        mode="existing",
        profile=PROFILE,
        subscription_id=options.subscription_id,
        location=None,
        agent_type=case.agent_type,
        name_prefix="insights-live-matrix",
        project_resource_id=fixture.resources.project_resource_id,
        project_endpoint=fixture.resources.project_endpoint,
        application_insights_resource_id=fixture.resources.application_insights_resource_id,
        model_deployment_name=fixture.resources.model_deployment_name,
        create_sample_agent=True,
        enable_existing_monitor=False,
    )


def _count(summary: Mapping[str, Any], name: str) -> int:
    value = summary.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OnboardingError(
            "live_matrix_invalid_result", "The result contains an invalid count.", {"field": name}
        )
    return int(value)


def validate_case_result(
    case: LiveMatrixCase,
    result: dict[str, Any],
) -> dict[str, Any]:
    if result.get("status") not in {"review_pending", "complete"}:
        raise OnboardingError(
            "live_matrix_incomplete_result",
            "The onboarding case did not complete technical execution.",
            {"case": case.name},
        )
    agent = result.get("agent")
    summary = result.get("result_summary")
    if not isinstance(agent, dict) or agent.get("kind") != case.agent_type:
        raise OnboardingError("live_matrix_agent_mismatch", "The result used the wrong Agent kind.")
    if not isinstance(summary, dict):
        raise OnboardingError("live_matrix_invalid_result", "The result summary is missing.")
    if summary.get("first_run_trigger") != "manual":
        raise OnboardingError(
            "live_matrix_trigger_mismatch", "Bug-bash cases require exactly one manual first run."
        )
    if summary.get("schedule_enabled") is not False:
        raise OnboardingError(
            "live_matrix_schedule_mismatch", "Bug-bash sample monitors must remain disabled."
        )
    if "/monitor/insights?" not in str(result.get("agent_insights_portal_url") or ""):
        raise OnboardingError(
            "live_matrix_portal_mismatch", "The result does not link directly to the Insights tab."
        )
    insight_count = _count(summary, "insight_count")
    prompt_count = _count(summary, "concrete_prompt_fix_count")
    code_count = _count(summary, "concrete_code_fix_count")
    findings: list[dict[str, Any]] = []
    if insight_count == 0:
        findings.append({"code": "empty_insights", "category": "quality"})
    elif (prompt_count if case.agent_type == "prompt" else code_count) == 0:
        findings.append(
            {
                "code": "missing_concrete_fix",
                "category": "quality",
                "surface": "system_prompt" if case.agent_type == "prompt" else "source_code",
            }
        )
    quality_review = result.get("quality_review", {})
    if not isinstance(quality_review, dict):
        raise OnboardingError("live_matrix_invalid_result", "Quality review metadata is invalid.")
    structural_count = _count(quality_review, "structural_finding_count")
    if structural_count:
        findings.append(
            {"code": "review_structural_findings", "category": "quality", "count": structural_count}
        )
    assertion: dict[str, Any] = {
        "status": "review_pending",
        "execution_status": "complete",
        "first_run_trigger": "manual",
        "schedule_enabled": False,
        "insight_count": insight_count,
        "concrete_prompt_fix_count": prompt_count,
        "concrete_code_fix_count": code_count,
        "quality_findings": findings,
        "quality_review": copy.deepcopy(quality_review),
        "evidence_status": quality_review.get("evidence_status", "not_reported"),
        "collection_status": quality_review.get("collection_status", "not_reported"),
        "ai_review_status": "not_performed_by_matrix",
        "human_review_status": "not_collected_by_matrix",
    }
    ensure_secret_free(assertion)
    return assertion


def _new_run_id() -> str:
    return secrets.token_hex(6)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _seal(payload: dict[str, Any], field: str) -> dict[str, Any]:
    result = {key: value for key, value in payload.items() if key != field}
    result[field] = _digest(result)
    ensure_secret_free(result)
    return result


def _read_sealed(path: Path, field: str, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OnboardingError(code, "The matrix evidence file is missing or invalid.") from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get(field), str)
        or value[field] != _digest({key: child for key, child in value.items() if key != field})
    ):
        raise OnboardingError(code, "The matrix evidence digest does not match its content.")
    ensure_secret_free(value)
    return value


def _validate_execution_id(value: str) -> str:
    if _EXECUTION_ID.fullmatch(value) is None:
        raise OnboardingError("invalid_matrix_execution_id", "Invalid matrix execution ID.")
    return value


def _source_provenance(root: Path = _REPO_ROOT) -> dict[str, Any]:
    skill = root / ".agents" / "skills" / "agent-insights-onboarding"
    files = {
        path
        for path in skill.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    files.update(root.glob("requirements*.lock"))
    files.update(
        path
        for path in (root / "pyproject.toml", root / ".github" / "workflows" / "live-matrix.yml")
        if path.is_file()
    )
    hashes = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(files)
    ]
    try:
        completed = subprocess.run(
            ["git", "--no-pager", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        revision = completed.stdout.strip() if completed.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        revision = None
    return {"git_commit": revision, "content_sha256": _digest(hashes), "files": hashes}


def _matrix_provenance(options: LiveMatrixOptions) -> dict[str, Any]:
    from .quality_review import RUBRIC_VERSION, SCHEMA_VERSION

    return {
        "matrix_schema_version": MATRIX_SCHEMA_VERSION,
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "profile": PROFILE,
        "review_schema_version": SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "runtime": {
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
        },
        "cases": [asdict(case) for case in options.cases],
        "configuration": {
            "subscription_id": options.subscription_id,
            "location": options.location,
            "model_name": options.model_name,
            "model_version": options.model_version,
            "model_format": options.model_format,
            "model_sku": options.model_sku,
            "model_capacity": options.model_capacity,
            "ingestion_timeout_seconds": options.ingestion_timeout_seconds,
            "insights_timeout_seconds": options.insights_timeout_seconds,
        },
        # Hash actual files, not just HEAD: dirty source, baselines, rubric and schema all matter.
        "source": _source_provenance(),
    }


def _matrix_configuration_fingerprint(options: LiveMatrixOptions) -> str:
    return _digest(_matrix_provenance(options))


def _resume_results(
    path: Path | None,
    options: LiveMatrixOptions,
    fingerprint: str,
) -> list[dict[str, Any]]:
    if path is None:
        return []
    value = _read_sealed(path, "summary_sha256", "invalid_live_matrix_resume")
    expected = [case.name for case in options.cases]
    if (
        value.get("schema_version") != MATRIX_SCHEMA_VERSION
        or value.get("profile") != PROFILE
        or value.get("expected_cases") != expected
        or value.get("configuration_fingerprint") != fingerprint
        or _digest(value.get("provenance")) != fingerprint
    ):
        raise OnboardingError(
            "live_matrix_resume_configuration_mismatch",
            "Resume requires the same profile, cases, source, baselines, review schema and config.",
        )
    if value.get("status") not in {"review_pending", "failed", "cancelled", "incomplete"}:
        raise OnboardingError("invalid_live_matrix_resume", "Resume requires a final summary.")
    if value.get("cleanup_status") != "complete":
        raise OnboardingError(
            "invalid_live_matrix_resume_cleanup", "Resume requires confirmed matrix cleanup."
        )
    previous = value.get("cases")
    if not isinstance(previous, list):
        raise OnboardingError("invalid_live_matrix_resume", "The summary has no valid case list.")
    carried: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in previous:
        if not isinstance(item, dict) or item.get("case") not in expected or item["case"] in seen:
            raise OnboardingError("invalid_live_matrix_resume", "Invalid or duplicate resume case.")
        seen.add(item["case"])
        if item.get("execution_status") != "complete":
            continue
        # Confirmed matrix cleanup covers a failed local cleanup without invalidating
        # the completed quality evidence or requiring replacement sample traffic.
        if (
            item.get("cleanup_status") not in {"complete", "not_needed", "failed", "cancelled"}
            or not isinstance(item.get("assertion"), dict)
            or item["assertion"].get("execution_status") != "complete"
            or item["assertion"].get("status") != "review_pending"
        ):
            raise OnboardingError(
                "invalid_live_matrix_resume", "A carried case lacks valid evidence."
            )
        retained = copy.deepcopy(item)
        source = {
            "execution_id": value.get("execution_id"),
            "summary_sha256": value["summary_sha256"],
            "summary_path": str(path),
        }
        retained["reused_from"] = source
        retained.setdefault("reuse_history", []).append(copy.deepcopy(source))
        carried.append(retained)
    return carried


def _manifest_error(message: str) -> OnboardingError:
    return OnboardingError("invalid_cleanup_manifest", message)


def _validate_manifest(
    value: dict[str, Any],
    *,
    execution_id: str,
    subscription_id: str,
) -> None:
    if (
        value.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or value.get("profile") != PROFILE
        or value.get("execution_id") != _validate_execution_id(execution_id)
        or str(value.get("subscription_id", "")).casefold() != subscription_id.casefold()
    ):
        raise _manifest_error(
            "The manifest belongs to a different execution, profile or subscription."
        )
    try:
        for field in ("subscription_id", "tenant_id", "owner_object_id"):
            UUID(value[field])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise _manifest_error("The manifest has an invalid Azure identity.") from error
    if (
        not isinstance(value.get("configuration_fingerprint"), str)
        or _SHA256.fullmatch(value["configuration_fingerprint"]) is None
        or not isinstance(value.get("runs"), list)
        or not isinstance(value.get("resource_groups"), list)
        or not isinstance(value.get("cleanup_status"), str)
        or value.get("cleanup_status") not in {"pending", "complete", "failed"}
    ):
        raise _manifest_error("The manifest lacks its configuration digest or resource lists.")
    expected_cases = value.get("expected_cases")
    if (
        not isinstance(expected_cases, list)
        or not expected_cases
        or any(not isinstance(case, str) or case not in CASES_BY_NAME for case in expected_cases)
        or len(set(expected_cases)) != len(expected_cases)
        or len(value["runs"]) > len(expected_cases) + 1
    ):
        raise _manifest_error("The manifest has an invalid or unbounded case selection.")
    runs: dict[str, dict[str, Any]] = {}
    registered_cases: set[str] = set()
    for item in value["runs"]:
        if not isinstance(item, dict) or not isinstance(item.get("run_id"), str):
            raise _manifest_error("Invalid run registration.")
        run_id = validate_run_id(item["run_id"])
        case = item.get("case")
        if (
            not isinstance(case, str)
            or run_id in runs
            or case in registered_cases
            or (case != "__fixture__" and case not in expected_cases)
            or (
                case == "__fixture__"
                and not any(CASES_BY_NAME[name].mode == "existing" for name in expected_cases)
            )
        ):
            raise _manifest_error("Duplicate run ID or unknown case.")
        expected = case == "__fixture__" or CASES_BY_NAME[case].mode == "scratch"
        if item.get("resource_group_expected") is not expected or any(
            type(item.get(field)) is not bool
            for field in ("creation_started", "resource_group_observed")
        ):
            raise _manifest_error("Invalid resource observation state.")
        if item["resource_group_observed"] and not item["creation_started"]:
            raise _manifest_error("An observed resource is missing its creation-start record.")
        runs[run_id] = item
        registered_cases.add(case)
    observed: set[str] = set()
    for group in value["resource_groups"]:
        if not isinstance(group, dict):
            raise _manifest_error("Invalid resource group entry.")
        group_run = group.get("run_id")
        if not isinstance(group_run, str) or group_run not in runs or group_run in observed:
            raise _manifest_error("Resource group has an unregistered or duplicate run ID.")
        run = runs[group_run]
        prefix = _GROUP_PREFIXES[0] if run["case"] == "__fixture__" else _GROUP_PREFIXES[1]
        expected_name = f"{prefix}{group_run}"
        expected_id = f"/subscriptions/{subscription_id}/resourceGroups/{expected_name}"
        if (
            not run["resource_group_expected"]
            or not run["resource_group_observed"]
            or group.get("name") != expected_name
            or str(group.get("resource_group_id", "")).casefold() != expected_id.casefold()
            or group.get("owner_object_id") != value["owner_object_id"]
            or group.get("subscription_id") != value["subscription_id"]
            or not isinstance(group.get("cleanup_status"), str)
            or group.get("cleanup_status") not in {"pending", "deleted", "already_absent", "failed"}
        ):
            raise _manifest_error(
                "Resource group scope or ownership does not match its registered run."
            )
        observed.add(group_run)
    if any(run["resource_group_observed"] != (run_id in observed) for run_id, run in runs.items()):
        raise _manifest_error("The resource observation ledger is incomplete.")


class CleanupManifest:
    def __init__(
        self,
        path: Path,
        options: LiveMatrixOptions,
        context: AzureContext,
        fingerprint: str,
    ) -> None:
        self.path = path
        self.payload: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "execution_id": options.execution_id,
            "profile": PROFILE,
            "subscription_id": context.subscription_id,
            "tenant_id": context.tenant_id,
            "owner_object_id": context.user_object_id,
            "configuration_fingerprint": fingerprint,
            "expected_cases": [case.name for case in options.cases],
            "created_at": _now(),
            "runs": [],
            "resource_groups": [],
            "cleanup_status": "pending",
        }
        self.save()

    def save(self) -> None:
        self.payload = _seal(self.payload, "manifest_sha256")
        write_json_atomic(self.path, self.payload)

    def register(self, run_id: str, case: str, *, resource_group_expected: bool) -> None:
        if any(item["run_id"] == run_id or item["case"] == case for item in self.payload["runs"]):
            raise _manifest_error("A run ID or case was already registered.")
        self.payload["runs"].append(
            {
                "run_id": validate_run_id(run_id),
                "case": case,
                "resource_group_expected": resource_group_expected,
                "creation_started": False,
                "resource_group_observed": False,
            }
        )
        self.save()

    def started(self, run_id: str) -> None:
        run = next(item for item in self.payload["runs"] if item["run_id"] == run_id)
        if not run["creation_started"]:
            run["creation_started"] = True
            self.save()

    def observer(self, run_id: str, config: OnboardingConfig) -> ResourceObserver:
        def observe(kind: str, value: Mapping[str, Any]) -> None:
            if kind != "resource_group":
                return
            run = next(item for item in self.payload["runs"] if item["run_id"] == run_id)
            name = resource_group_name(config, run_id)
            identifier = str(value.get("id") or "")
            expected = f"/subscriptions/{self.payload['subscription_id']}/resourceGroups/{name}"
            if (
                not run["resource_group_expected"]
                or identifier.casefold() != expected.casefold()
                or not name.startswith(_GROUP_PREFIXES)
            ):
                raise _manifest_error("The provisioning observer returned an unexpected group.")
            require_owned_tags(
                value.get("tags"), run_id=run_id, owner_object_id=self.payload["owner_object_id"]
            )
            if not run["resource_group_observed"]:
                self.payload["resource_groups"].append(
                    {
                        "resource_group_id": identifier,
                        "name": name,
                        "run_id": run_id,
                        "owner_object_id": self.payload["owner_object_id"],
                        "subscription_id": self.payload["subscription_id"],
                        "cleanup_status": "pending",
                    }
                )
                run["creation_started"] = True
                run["resource_group_observed"] = True
                self.save()

        return observe


def cleanup_matrix_manifest(
    cli: AzureCli,
    path: Path,
    *,
    execution_id: str,
    subscription_id: str,
) -> dict[str, Any]:
    value = _read_sealed(path, "manifest_sha256", "invalid_cleanup_manifest")
    _validate_manifest(value, execution_id=execution_id, subscription_id=subscription_id)
    context = select_context(cli, subscription_id)
    if (
        context.user_object_id.casefold() != value["owner_object_id"].casefold()
        or context.tenant_id.casefold() != value["tenant_id"].casefold()
        or context.subscription_id.casefold() != value["subscription_id"].casefold()
    ):
        raise OnboardingError(
            "live_matrix_cleanup_identity_mismatch",
            "Cleanup must use the manifest's exact creating user, tenant and subscription; "
            "no resources were deleted.",
        )
    failures = [
        {"run_id": run["run_id"], "error_code": "resource_creation_not_observed"}
        for run in value["runs"]
        if run["resource_group_expected"]
        and run["creation_started"]
        and not run["resource_group_observed"]
    ]
    for group in value["resource_groups"]:
        try:
            arguments = [
                "--name", group["name"], "--subscription", subscription_id,
            ]
            exists = cli.json(["group", "exists", *arguments])
            if exists is False:
                group["cleanup_status"] = "already_absent"
            elif exists is True:
                live = cli.json(["group", "show", *arguments])
                if (
                    not isinstance(live, dict)
                    or str(live.get("id") or "").casefold()
                    != group["resource_group_id"].casefold()
                ):
                    raise _manifest_error("Live cleanup resolved a different resource group.")
                require_owned_tags(
                    live.get("tags"),
                    run_id=group["run_id"],
                    owner_object_id=value["owner_object_id"],
                )
                cleanup_scratch(
                    cli,
                    resource_group_id=group["resource_group_id"],
                    run_id=group["run_id"],
                    owner_object_id=value["owner_object_id"],
                )
                if cli.json(["group", "exists", *arguments]) is not False:
                    raise OnboardingError(
                        "live_matrix_cleanup_leak", "A manifest resource group still exists."
                    )
                group["cleanup_status"] = "deleted"
            else:
                raise _manifest_error("Azure returned an invalid group-existence result.")
        except Exception as error:
            group["cleanup_status"] = "failed"
            failures.append(
                {
                    "run_id": group["run_id"],
                    "error_code": error.code if isinstance(error, OnboardingError)
                    else type(error).__name__,
                }
            )
        value["cleanup_status"] = "pending"
        value = _seal(value, "manifest_sha256")
        write_json_atomic(path, value)
    value["cleanup_status"] = "failed" if failures else "complete"
    value["cleanup_failures"] = failures
    value["cleanup_checked_at"] = _now()
    write_json_atomic(path, _seal(value, "manifest_sha256"))
    result = {
        "status": value["cleanup_status"],
        "execution_id": execution_id,
        "resource_group_count": len(value["resource_groups"]),
        "resource_groups": value["resource_groups"],
        "failures": failures,
    }
    if failures:
        raise OnboardingError(
            "live_matrix_cleanup_incomplete",
            "Some manifest resources were not verified absent; cleanup was not successful.",
            result,
        )
    return result


def _fixture_config(options: LiveMatrixOptions) -> OnboardingConfig:
    return OnboardingConfig(
        mode="scratch",
        profile=PROFILE,
        subscription_id=options.subscription_id,
        location=options.location,
        agent_type="hosted" if any(
            case.mode == "existing" and case.agent_type == "hosted" for case in options.cases
        ) else "prompt",
        name_prefix="insights-live-fixture",
        model_name=options.model_name,
        model_version=options.model_version,
        model_format=options.model_format,
        model_sku=options.model_sku,
        model_capacity=options.model_capacity,
    )


def _create_fixture(
    cli: AzureCli,
    options: LiveMatrixOptions,
    context: AzureContext,
    manifest: CleanupManifest,
) -> FixtureProject:
    run_id = _new_run_id()
    config = _fixture_config(options)
    manifest.register(run_id, "__fixture__", resource_group_expected=True)
    orchestrator.doctor(config, cli)
    manifest.started(run_id)
    resources = provision_scratch(
        cli,
        config=config,
        context=context,
        run_id=run_id,
        resource_observer=manifest.observer(run_id, config),
    )
    return FixtureProject(run_id, config, context, resources)


def _cleanup_case(run_id: str, *, cli: AzureCli) -> str:
    run_dir = orchestrator._run_dir(run_id)
    if not any(
        (run_dir / name).exists()
        for name in ("provisioning-state.json", "provisioning-receipt.json")
    ):
        return "not_needed"
    orchestrator.cleanup(run_dir, cli=cli)
    return "complete"


def _safe_error(error: BaseException, stage: str) -> dict[str, Any]:
    return {
        "code": error.code if isinstance(error, OnboardingError)
        else "live_matrix_unexpected_failure",
        "exception_type": type(error).__name__,
        "stage": stage,
    }


def _run_case(
    case: LiveMatrixCase,
    options: LiveMatrixOptions,
    *,
    cli: AzureCli,
    fixture: FixtureProject | None,
    manifest: CleanupManifest,
    checkpoint: Callable[[str, str | None], None],
    fingerprint: str,
) -> dict[str, Any]:
    started = time.monotonic()
    run_id = _new_run_id()
    config = case_config(case, options, fixture=fixture)
    manifest.register(run_id, case.name, resource_group_expected=case.mode == "scratch")
    assertion: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    cleanup_error: dict[str, Any] | None = None
    status = "review_pending"
    execution_status = "running"
    cleanup_status = "not_started"
    stage = "preflight"

    def progress(value: dict[str, Any]) -> None:
        nonlocal stage
        stage = str(value.get("stage") or value.get("status") or "onboarding")
        if stage == "provisioning":
            manifest.started(run_id)
        checkpoint(stage, case.name)

    try:
        checkpoint(stage, case.name)
        result = orchestrator.onboard(
            config,
            run_id=run_id,
            ingestion_timeout_seconds=options.ingestion_timeout_seconds,
            insights_timeout_seconds=options.insights_timeout_seconds,
            cli=cli,
            progress_callback=progress,
            resource_observer=manifest.observer(run_id, config),
        )
        assertion = validate_case_result(case, result)
        execution_status = "complete"
    except (KeyboardInterrupt, SystemExit) as interrupted:
        status = "cancelled"
        execution_status = "cancelled"
        error = {"code": "live_matrix_cancelled", "stage": stage,
                 "exception_type": type(interrupted).__name__}
        checkpoint("cancelled", case.name)
    except Exception as failure:
        status = "failed"
        execution_status = "failed"
        error = _safe_error(failure, stage)
    finally:
        try:
            checkpoint("case_cleanup", case.name)
            cleanup_status = _cleanup_case(run_id, cli=cli)
        except (KeyboardInterrupt, SystemExit) as interrupted:
            status = "cancelled"
            cleanup_status = "cancelled"
            cleanup_error = {
                "code": "live_matrix_cancelled", "stage": "case_cleanup",
                "exception_type": type(interrupted).__name__,
            }
            checkpoint("cancelled", case.name)
        except Exception as failure:
            cleanup_status = "failed"
            cleanup_error = _safe_error(failure, "case_cleanup")
    return {
        "case": case.name,
        "run_id": run_id,
        "run_dir": str(orchestrator._run_dir(run_id)),
        "status": status,
        "execution_status": execution_status,
        "duration_seconds": round(time.monotonic() - started, 2),
        "cleanup_status": cleanup_status,
        "assertion": assertion,
        "error": error,
        "cleanup_error": cleanup_error,
        "provenance": {
            "execution_id": options.execution_id,
            "configuration_fingerprint": fingerprint,
            "recorded_at": _now(),
        },
    }


def _summary_payload(
    results: list[dict[str, Any]],
    started: float,
    options: LiveMatrixOptions,
    *,
    provenance: dict[str, Any],
    state: str,
    stage: str,
    current_case: str | None,
    cleanup_status: str,
    matrix_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = [case.name for case in options.cases]
    completed = sum(item.get("execution_status") in {"complete", "failed"} for item in results)
    failed = sum(item.get("status") == "failed" for item in results)
    if state == "finished":
        if matrix_error or failed or cleanup_status != "complete":
            state = "failed"
        elif not expected or completed != len(expected) or {
            item.get("case") for item in results
        } != set(expected):
            state = "incomplete"
        else:
            state = "review_pending"
    summary: dict[str, Any] = {
        "schema_version": MATRIX_SCHEMA_VERSION,
        "profile": PROFILE,
        "execution_id": options.execution_id,
        "status": state,
        "execution_status": "complete" if state == "review_pending" else state,
        "expected_cases": expected,
        "expected_case_count": len(expected),
        "completed_case_count": completed,
        "case_count": len(results),
        "failed_case_count": failed,
        "current_case": current_case,
        "stage": stage,
        "cleanup_status": cleanup_status,
        "duration_seconds": round(time.monotonic() - started, 2),
        "configuration_fingerprint": _digest(provenance),
        "provenance": provenance,
        "cleanup_manifest_path": str(options.output_dir / "cleanup-manifest.json"),
        "quality_status": "findings" if any(
            item.get("assertion", {}).get("quality_findings") for item in results
        ) else "review_pending",
        "ai_review_status": "not_performed_by_matrix",
        "human_review_status": "not_collected_by_matrix",
        "matrix_error": matrix_error,
        "cases": results,
    }
    return _seal(summary, "summary_sha256")


def run_live_matrix(
    options: LiveMatrixOptions,
    *,
    cli: AzureCli | None = None,
) -> dict[str, Any]:
    if not options.cases or len({case.name for case in options.cases}) != len(options.cases):
        raise OnboardingError(
            "invalid_live_matrix_cases", "Select at least one distinct matrix case."
        )
    if any(case not in CASES_BY_NAME.values() for case in options.cases):
        raise OnboardingError("unknown_live_matrix_case", "Unsupported bug-bash case.")
    if any(
        not math.isfinite(timeout) or timeout <= 0
        for timeout in (options.ingestion_timeout_seconds, options.insights_timeout_seconds)
    ):
        raise OnboardingError(
            "invalid_live_matrix_timeout", "Matrix timeouts must be positive and finite."
        )
    options = replace(
        options, execution_id=_validate_execution_id(options.execution_id or _new_run_id())
    )
    summary_path = options.output_dir / "summary.json"
    manifest_path = options.output_dir / "cleanup-manifest.json"
    if summary_path.exists() or manifest_path.exists():
        raise OnboardingError(
            "live_matrix_output_exists",
            "Use a fresh output directory; existing evidence is immutable.",
        )
    provenance = _matrix_provenance(options)
    fingerprint = _digest(provenance)
    results = _resume_results(options.resume_summary, options, fingerprint)
    resumed = {item["case"] for item in results}
    try:
        options.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as failure:
        raise OnboardingError(
            "live_matrix_output_exists",
            "A matrix execution already reserved this output directory.",
        ) from failure
    started = time.monotonic()
    stage = "initializing"
    current_case: str | None = None
    cleanup_status = "pending"
    state = "running"
    matrix_error: dict[str, Any] | None = None
    manifest: CleanupManifest | None = None
    selected_cli = cli or AzureCli()

    def checkpoint(new_stage: str, case_name: str | None) -> None:
        nonlocal stage, current_case, state
        stage, current_case = new_stage, case_name
        if new_stage == "cancelled":
            state = "cancelled"
        write_json_atomic(
            summary_path,
            _summary_payload(
                results, started, options, provenance=provenance,
                state=state if state in {"cancelled", "failed"} else "running",
                stage=stage, current_case=current_case, cleanup_status=cleanup_status,
                matrix_error=matrix_error,
            ),
        )
        print(
            json.dumps({
                "event": "matrix_progress", "execution_id": options.execution_id,
                "stage": stage, "current_case": current_case,
                "completed_case_count": sum(
                    item.get("execution_status") in {"complete", "failed"} for item in results
                ),
                "expected_case_count": len(options.cases),
                "summary_path": str(summary_path),
            }),
            flush=True,
        )

    checkpoint(stage, None)
    try:
        context = select_context(selected_cli, options.subscription_id)
        manifest = CleanupManifest(manifest_path, options, context, fingerprint)
        fixture: FixtureProject | None = None
        for case in options.cases:
            if case.name in resumed:
                continue
            if case.mode == "existing" and fixture is None:
                checkpoint("fixture_provisioning", case.name)
                fixture = _create_fixture(selected_cli, options, context, manifest)
            checkpoint("case_start", case.name)
            result = _run_case(
                case, options, cli=selected_cli, fixture=fixture,
                manifest=manifest, checkpoint=checkpoint, fingerprint=fingerprint,
            )
            results.append(result)
            if result["status"] == "cancelled":
                state = "cancelled"
                matrix_error = result.get("error") or result.get("cleanup_error")
                break
            checkpoint("between_cases", None)
    except (KeyboardInterrupt, SystemExit) as interrupted:
        state = "cancelled"
        matrix_error = {"code": "live_matrix_cancelled", "stage": stage,
                        "exception_type": type(interrupted).__name__}
    except Exception as failure:
        state = "failed"
        matrix_error = _safe_error(failure, stage)
    finally:
        if manifest is not None:
            try:
                checkpoint("matrix_cleanup", current_case)
                cleanup_matrix_manifest(
                    selected_cli, manifest_path, execution_id=options.execution_id or "",
                    subscription_id=options.subscription_id,
                )
                cleanup_status = "complete"
            except (KeyboardInterrupt, SystemExit):
                state = "cancelled"
                cleanup_status = "cancelled"
            except Exception as failure:
                cleanup_status = "failed"
                matrix_error = matrix_error or _safe_error(failure, "matrix_cleanup")
        else:
            cleanup_status = "not_started"
    summary = _summary_payload(
        results, started, options, provenance=provenance,
        state="finished" if state == "running" else state,
        stage="finished" if state == "running" else stage,
        current_case=None if state == "running" else current_case,
        cleanup_status=cleanup_status, matrix_error=matrix_error,
    )
    write_json_atomic(summary_path, summary)
    return summary


@contextmanager
def _cancellation_handler() -> Iterator[None]:
    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Opt-in disposable bug-bash quality smoke matrix. 'all' runs only the two "
            "existing-project/new-sample cases, using a matrix-owned prepared fixture. "
            "Every case sends the fixed 11 sample requests and uses one manual Insights run. "
            "All resources are automatically cleaned; this is not participant human acceptance."
        ),
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument(
        "--cleanup-manifest", type=Path,
        help="Clean only exact recorded groups; requires --execution-id and --subscription-id.",
    )
    parser.add_argument(
        "--list-cases", action="store_true",
        help="List primary cases and separately selectable scratch fallbacks without Azure.",
    )
    parser.add_argument(
        "--cases", default=os.getenv("AGENT_INSIGHTS_LIVE_CASES", "all"),
        help="'all' = primary existing cases only. Scratch fallbacks must be named explicitly: "
        + ", ".join(case.name for case in FALLBACK_CASES),
    )
    parser.add_argument(
        "--execution-id", help="Execution identity; required when cleaning a manifest."
    )
    parser.add_argument(
        "--subscription-id", default=os.getenv("AGENT_INSIGHTS_LIVE_SUBSCRIPTION_ID")
    )
    parser.add_argument("--location", default=os.getenv("AGENT_INSIGHTS_LIVE_LOCATION"))
    parser.add_argument(
        "--model-name", default=os.getenv("AGENT_INSIGHTS_LIVE_MODEL_NAME") or "gpt-5.4"
    )
    parser.add_argument(
        "--model-version", default=os.getenv("AGENT_INSIGHTS_LIVE_MODEL_VERSION") or "2026-03-05"
    )
    parser.add_argument("--model-format", default="OpenAI")
    parser.add_argument("--model-sku", default="GlobalStandard")
    parser.add_argument("--model-capacity", type=int, default=30)
    parser.add_argument(
        "--output-dir", type=Path,
        help="Fresh output directory; never overwrites existing summary or cleanup evidence.",
    )
    parser.add_argument(
        "--resume-summary", type=Path,
        help="Reuse matching completed-case evidence; retry other cases with fresh owned samples "
        "only after confirmed cleanup, never replay an old Agent.",
    )
    parser.add_argument("--ingestion-timeout-seconds", type=float, default=900)
    parser.add_argument("--insights-timeout-seconds", type=float, default=2400)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_cases:
        print(json.dumps({
            "profile": PROFILE,
            "cases": list(supported_case_names()),
            "fallback_cases": [case.name for case in FALLBACK_CASES],
        }, indent=2))
        return 0
    if not args.confirm_live:
        raise OnboardingError(
            "live_confirmation_required",
            "Pass --confirm-live to acknowledge disposable Azure writes, model usage and cleanup.",
        )
    if not args.subscription_id:
        raise OnboardingError(
            "live_matrix_configuration_missing", "The matrix requires --subscription-id."
        )
    if args.cleanup_manifest:
        if not args.execution_id:
            raise OnboardingError(
                "live_matrix_configuration_missing", "Manifest cleanup requires --execution-id."
            )
        result = cleanup_matrix_manifest(
            AzureCli(), args.cleanup_manifest,
            execution_id=args.execution_id, subscription_id=args.subscription_id,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if not args.location:
        raise OnboardingError(
            "live_matrix_configuration_missing", "Execution requires --location for owned fixtures."
        )
    execution_id = _validate_execution_id(args.execution_id or _new_run_id())
    output_dir = args.output_dir or Path(".agent-insights") / "live-matrix" / execution_id
    options = LiveMatrixOptions(
        subscription_id=args.subscription_id,
        location=args.location,
        model_name=args.model_name,
        model_version=args.model_version,
        model_format=args.model_format,
        model_sku=args.model_sku,
        model_capacity=args.model_capacity,
        cases=select_cases(args.cases),
        output_dir=output_dir.resolve(),
        ingestion_timeout_seconds=args.ingestion_timeout_seconds,
        insights_timeout_seconds=args.insights_timeout_seconds,
        resume_summary=args.resume_summary,
        execution_id=execution_id,
    )
    with _cancellation_handler():
        summary = run_live_matrix(options)
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return 0 if summary["execution_status"] == "complete" else 1
