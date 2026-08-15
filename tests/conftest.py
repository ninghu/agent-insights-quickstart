from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / ".agents" / "skills" / "agent-insights-onboarding"
SCRIPTS_ROOT = SKILL_ROOT / "scripts"
ASSETS_ROOT = SKILL_ROOT / "assets"

if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from insights_onboarding.models import (  # noqa: E402
    AgentDeployment,
    AzureContext,
    OnboardingConfig,
    ProjectResources,
)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def assets_root() -> Path:
    return ASSETS_ROOT


@pytest.fixture
def run_id() -> str:
    return "abc123def456"


@pytest.fixture
def subscription_id() -> str:
    return "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def azure_ids(subscription_id: str) -> dict[str, str]:
    rg = "rg-agent-insights"
    account = (
        f"/subscriptions/{subscription_id}/resourceGroups/{rg}"
        "/providers/Microsoft.CognitiveServices/accounts/demo-account"
    )
    project = f"{account}/projects/demo-project"
    app_insights = (
        f"/subscriptions/{subscription_id}/resourceGroups/{rg}"
        "/providers/Microsoft.Insights/components/demo-appi"
    )
    workspace = (
        f"/subscriptions/{subscription_id}/resourceGroups/{rg}"
        "/providers/Microsoft.OperationalInsights/workspaces/demo-law"
    )
    return {
        "resource_group": rg,
        "account": account,
        "project": project,
        "app_insights": app_insights,
        "workspace": workspace,
    }


@pytest.fixture
def azure_context(subscription_id: str) -> AzureContext:
    return AzureContext(
        cloud="AzureCloud",
        subscription_id=subscription_id,
        subscription_name="Demo Subscription",
        tenant_id="22222222-2222-2222-2222-222222222222",
        user_name="person@example.com",
        user_type="user",
        user_object_id="33333333-3333-3333-3333-333333333333",
    )


@pytest.fixture
def make_config(subscription_id: str):
    def _make(**overrides: object) -> OnboardingConfig:
        values: dict[str, object] = {
            "mode": "scratch",
            "subscription_id": subscription_id,
            "location": "westus3",
            "agent_type": "prompt",
            "name_prefix": "agent-insights",
            "model_name": "gpt-4.1-mini",
            "model_version": "2025-04-14",
            "model_format": "OpenAI",
            "model_sku": "GlobalStandard",
            "model_capacity": 1,
            "invoke_existing_agent": False,
            "enable_existing_monitor": False,
            "protected_trace_content": False,
        }
        values.update(overrides)
        return OnboardingConfig(**values)

    return _make


@pytest.fixture
def make_resources(azure_ids: dict[str, str]):
    def _make(**overrides: object) -> ProjectResources:
        values: dict[str, object] = {
            "project_resource_id": azure_ids["project"],
            "project_endpoint": (
                "https://demo-account.services.ai.azure.com/api/projects/demo-project"
            ),
            "project_principal_id": "44444444-4444-4444-4444-444444444444",
            "foundry_account_resource_id": azure_ids["account"],
            "application_insights_resource_id": azure_ids["app_insights"],
            "log_analytics_workspace_resource_id": azure_ids["workspace"],
            "model_deployment_name": "gpt-4-1-mini",
            "resource_group_id": (
                "/subscriptions/11111111-1111-1111-1111-111111111111"
                "/resourceGroups/rg-agent-insights"
            ),
        }
        values.update(overrides)
        return ProjectResources(**values)

    return _make


@pytest.fixture
def make_deployment():
    def _make(**overrides: object) -> AgentDeployment:
        values: dict[str, object] = {
            "name": "sample-agent",
            "version": "1.2.3",
            "kind": "prompt",
            "artifact_sha256": None,
        }
        values.update(overrides)
        return AgentDeployment(**values)

    return _make
