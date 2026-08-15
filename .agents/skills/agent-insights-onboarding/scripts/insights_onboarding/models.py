"""Serializable onboarding state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

Mode = Literal["scratch", "existing"]
AgentType = Literal["prompt", "hosted"]


@dataclass(frozen=True, slots=True)
class AzureContext:
    cloud: str
    subscription_id: str
    subscription_name: str
    tenant_id: str
    user_name: str
    user_type: str
    user_object_id: str


@dataclass(frozen=True, slots=True)
class OnboardingConfig:
    mode: Mode
    subscription_id: str
    location: str | None = None
    agent_type: AgentType | None = None
    name_prefix: str = "agent-insights"
    project_resource_id: str | None = None
    project_endpoint: str | None = None
    application_insights_resource_id: str | None = None
    agent_name: str | None = None
    model_deployment_name: str | None = None
    model_name: str = "gpt-4.1-mini"
    model_version: str = "2025-04-14"
    model_format: str = "OpenAI"
    model_sku: str = "GlobalStandard"
    model_capacity: int = 1
    lookback_hours: int = 168
    invoke_existing_agent: bool = False
    enable_existing_monitor: bool = False
    protected_trace_content: bool = True


@dataclass(frozen=True, slots=True)
class Mutation:
    kind: str
    target: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OnboardingPlan:
    schema_version: int
    run_id: str
    created_at: str
    mode: Mode
    config: dict[str, Any]
    azure_context: dict[str, Any]
    mutations: tuple[dict[str, Any], ...]
    expected: dict[str, Any]
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        config: OnboardingConfig,
        context: AzureContext,
        mutations: list[Mutation],
        expected: dict[str, Any],
    ) -> OnboardingPlan:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "mode": config.mode,
            "config": asdict(config),
            "azure_context": asdict(context),
            "mutations": [asdict(item) for item in mutations],
            "expected": expected,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return cls(
            schema_version=1,
            run_id=run_id,
            created_at=str(payload["created_at"]),
            mode=config.mode,
            config=payload["config"],
            azure_context=payload["azure_context"],
            mutations=tuple(payload["mutations"]),
            expected=expected,
            plan_hash=hashlib.sha256(canonical).hexdigest(),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectResources:
    project_resource_id: str
    project_endpoint: str
    project_principal_id: str
    foundry_account_resource_id: str
    application_insights_resource_id: str
    log_analytics_workspace_resource_id: str
    model_deployment_name: str
    resource_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentDeployment:
    name: str
    version: str
    kind: AgentType
    artifact_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TrafficOutcome:
    scenario: str
    expected_fault: bool
    response_id: str | None
    session_id: str | None
    trace_id: str | None
    started_at: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class MonitorOutcome:
    monitor_id: str
    run_id: str
    insight_ids: tuple[str, ...]
    estimated_cost: dict[str, Any] | None
    enabled: bool
