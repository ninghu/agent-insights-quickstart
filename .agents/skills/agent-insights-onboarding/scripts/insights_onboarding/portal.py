"""Stable Microsoft Foundry portal links."""

from __future__ import annotations

import base64
import uuid
from urllib.parse import quote, urlencode

from .resource_ids import require_resource_type


def _encoded_subscription_id(subscription_id: str) -> str:
    raw = uuid.UUID(subscription_id).bytes
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def foundry_project_prefix(project_resource_id: str) -> str:
    project = require_resource_type(
        project_resource_id,
        "Microsoft.CognitiveServices/accounts/projects",
    )
    subscription = _encoded_subscription_id(project.subscription_id)
    account_name, project_name = project.names
    context = ",".join(
        (
            subscription,
            quote(project.resource_group, safe=""),
            "",
            quote(account_name, safe=""),
            quote(project_name, safe=""),
        )
    )
    return f"https://ai.azure.com/nextgen/r/{context}"


def foundry_project_url(project_resource_id: str, tenant_id: str) -> str:
    return (
        f"{foundry_project_prefix(project_resource_id)}/home?"
        + urlencode({"tid": tenant_id})
    )


def agent_insights_url(
    project_resource_id: str,
    tenant_id: str,
    agent_name: str,
) -> str:
    return (
        f"{foundry_project_prefix(project_resource_id)}/build/agents/"
        f"{quote(agent_name, safe='')}/monitor/insights?"
        + urlencode({"tid": tenant_id})
    )
