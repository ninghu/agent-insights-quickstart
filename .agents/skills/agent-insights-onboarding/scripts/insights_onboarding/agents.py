"""Foundry agent creation and immutable version validation."""

from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from azure.core.exceptions import HttpResponseError

from .errors import OnboardingError
from .models import AgentDeployment, AgentType
from .validation import normalize_name

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ASSETS = _SKILL_ROOT / "assets" / "agents"
_OWNER_KEY = "agent_insights_quickstart_owner"
_RUN_KEY = "agent_insights_quickstart_run_id"
_OWNER_VALUE = "agent-insights-quickstart"


def project_client(endpoint: str, tenant_id: str) -> Any:
    projects = importlib.import_module("azure.ai.projects")
    identity = importlib.import_module("azure.identity")
    credential = identity.AzureCliCredential(tenant_id=tenant_id)
    return projects.AIProjectClient(
        endpoint=endpoint,
        credential=credential,
        allow_preview=True,
        retry_total=0,
    )


def agent_name(run_id: str, agent_type: AgentType) -> str:
    return normalize_name(f"insights-{agent_type}-{run_id}", max_length=48)


def _metadata(run_id: str) -> dict[str, str]:
    return {_OWNER_KEY: _OWNER_VALUE, _RUN_KEY: run_id}


def _definition_kind(version: object) -> str:
    definition = getattr(version, "definition", None)
    kind = getattr(definition, "kind", "")
    return str(getattr(kind, "value", kind) or "").casefold()


def _owned_version(version: object, run_id: str, expected_kind: str) -> bool:
    metadata = getattr(version, "metadata", None)
    return (
        isinstance(metadata, Mapping)
        and metadata.get(_OWNER_KEY) == _OWNER_VALUE
        and metadata.get(_RUN_KEY) == run_id
        and _definition_kind(version) == expected_kind.casefold()
    )


def _existing_version(project: Any, name: str, run_id: str, kind: str) -> object | None:
    try:
        versions = list(project.agents.list_versions(name, limit=100, order="desc"))
    except HttpResponseError as error:
        if error.status_code == 404:
            return None
        raise
    if not versions:
        return None
    owned = [version for version in versions if _owned_version(version, run_id, kind)]
    if len(owned) == 1:
        return cast(object, owned[0])
    if owned:
        raise OnboardingError(
            "duplicate_owned_agent_versions",
            "Multiple versions claim ownership for the same quickstart run.",
        )
    raise OnboardingError(
        "agent_ownership_mismatch",
        f"Agent '{name}' already exists and is not owned by this run.",
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OnboardingError(
            "invalid_agent_asset",
            f"Agent asset is invalid: {path.name}",
        ) from error


def _prompt_definition(model: str) -> Any:
    models = importlib.import_module("azure.ai.projects.models")
    root = _AGENT_ASSETS / "prompt-agent"
    instructions = (root / "instructions.txt").read_text(encoding="utf-8").strip()
    tool = _load_json(root / "lookup_order_tool.json")
    if not instructions or not isinstance(tool, dict):
        raise OnboardingError(
            "invalid_prompt_asset",
            "Prompt Agent assets are incomplete.",
        )
    return models.PromptAgentDefinition(
        model=model,
        instructions=instructions,
        tools=[tool],
    )


def _deterministic_zip(source: Path, destination: Path) -> str:
    files = sorted(
        path for path in source.iterdir() if path.is_file() and path.name != "agent_manifest.json"
    )
    if {path.name for path in files} != {
        "main.py",
        "requirements.txt",
        "healthy_requests.json",
        "faulty_requests.json",
    }:
        raise OnboardingError(
            "invalid_hosted_asset",
            "Hosted Agent source folder has an unexpected file set.",
        )
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _hosted_definition(model: str) -> Any:
    models = importlib.import_module("azure.ai.projects.models")
    return models.HostedAgentDefinition(
        cpu="1",
        memory="2Gi",
        code_configuration=models.CodeConfiguration(
            runtime="python_3_13",
            entry_point=["python", "main.py"],
            dependency_resolution="remote_build",
        ),
        protocol_versions=[
            models.ProtocolVersionRecord(protocol="responses", version="1.0.0")
        ],
        environment_variables={"AZURE_AI_MODEL_DEPLOYMENT_NAME": model},
    )


def _wait_active(
    project: Any,
    *,
    name: str,
    version: str,
    timeout_seconds: float,
) -> object:
    deadline = time.monotonic() + timeout_seconds
    while True:
        current = project.agents.get_version(name, version)
        status_value = getattr(current, "status", "")
        status = str(getattr(status_value, "value", status_value) or "").casefold()
        if status == "active":
            return current
        if status in {"failed", "deleted", "deleting"}:
            raise OnboardingError(
                "hosted_agent_deployment_failed",
                f"Hosted Agent version reached terminal state '{status}'.",
            )
        if time.monotonic() >= deadline:
            raise OnboardingError(
                "hosted_agent_deployment_timeout",
                "Hosted Agent version did not become active before timeout.",
            )
        time.sleep(10)


def create_sample_agent(
    project: Any,
    *,
    run_id: str,
    agent_type: AgentType,
    model: str,
    timeout_seconds: float = 1800,
) -> AgentDeployment:
    name = agent_name(run_id, agent_type)
    expected_kind = "prompt" if agent_type == "prompt" else "hosted"
    existing = _existing_version(project, name, run_id, expected_kind)
    if existing is not None:
        version = str(getattr(existing, "version", "") or "")
        if not version:
            raise OnboardingError(
                "invalid_agent_version",
                "Existing owned Agent version has no version identifier.",
            )
        return AgentDeployment(name=name, version=version, kind=agent_type)
    if agent_type == "prompt":
        created = project.agents.create_version(
            agent_name=name,
            definition=_prompt_definition(model),
            metadata=_metadata(run_id),
            description="Agent Insights Quickstart sample order-status Prompt Agent.",
        )
        version = str(getattr(created, "version", "") or "")
        if not version:
            raise OnboardingError(
                "invalid_agent_version",
                "Prompt Agent create response had no version.",
            )
        return AgentDeployment(name=name, version=version, kind=agent_type)
    with tempfile.TemporaryDirectory(prefix="agent-insights-quickstart-") as directory:
        archive_path = Path(directory) / "hosted-agent.zip"
        sha256 = _deterministic_zip(_AGENT_ASSETS / "hosted-agent", archive_path)
        content = archive_path.read_bytes()
        created = project.agents.create_version_from_code(
            agent_name=name,
            definition=_hosted_definition(model),
            code=(archive_path.name, content, "application/zip"),
            code_zip_sha256=sha256,
            metadata=_metadata(run_id),
            description="Agent Insights Quickstart sample order-status Hosted Agent.",
        )
        version = str(getattr(created, "version", "") or "")
        if not version:
            raise OnboardingError(
                "invalid_agent_version",
                "Hosted Agent create response had no version.",
            )
        _wait_active(
            project,
            name=name,
            version=version,
            timeout_seconds=timeout_seconds,
        )
        return AgentDeployment(
            name=name,
            version=version,
            kind=agent_type,
            artifact_sha256=sha256,
        )


def delete_owned_agent(
    project: Any,
    *,
    deployment: AgentDeployment,
    run_id: str,
) -> None:
    expected_name = agent_name(run_id, deployment.kind)
    if deployment.name != expected_name:
        raise OnboardingError(
            "agent_cleanup_target_mismatch",
            "Agent cleanup target does not match the deterministic quickstart name.",
        )
    try:
        versions = list(
            project.agents.list_versions(
                deployment.name,
                limit=100,
                order="desc",
            )
        )
    except HttpResponseError as error:
        if error.status_code == 404:
            raise OnboardingError(
                "agent_cleanup_target_missing",
                "The quickstart-owned Agent no longer exists.",
            ) from error
        raise
    if (
        len(versions) != 1
        or str(getattr(versions[0], "version", "") or "") != deployment.version
        or not _owned_version(versions[0], run_id, deployment.kind)
    ):
        raise OnboardingError(
            "agent_cleanup_target_mismatch",
            "Live Agent versions no longer match the quickstart ownership receipt.",
        )
    project.agents.delete(deployment.name, force=True)


def validate_existing_agent(project: Any, *, name: str) -> AgentDeployment:
    try:
        versions = list(project.agents.list_versions(name, limit=1, order="desc"))
    except HttpResponseError as error:
        if error.status_code == 404:
            raise OnboardingError(
                "agent_not_found",
                f"Agent '{name}' was not found in the selected project.",
            ) from error
        raise
    if not versions:
        raise OnboardingError(
            "agent_not_found",
            f"Agent '{name}' has no immutable version.",
        )
    version = versions[0]
    version_id = str(getattr(version, "version", "") or "")
    kind = _definition_kind(version)
    if kind not in {"prompt", "hosted"} or not version_id:
        raise OnboardingError(
            "unsupported_agent",
            "Existing quickstart path currently supports Prompt and Hosted agents.",
            {"kind": kind},
        )
    return AgentDeployment(name=name, version=version_id, kind=kind)  # type: ignore[arg-type]
