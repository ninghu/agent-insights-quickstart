from __future__ import annotations

from insights_onboarding.portal import agent_insights_url, foundry_project_url


def test_foundry_portal_links_encode_project_context(
    azure_ids: dict[str, str],
) -> None:
    project_url = foundry_project_url(
        azure_ids["project"],
        "22222222-2222-2222-2222-222222222222",
    )
    insights_url = agent_insights_url(
        azure_ids["project"],
        "22222222-2222-2222-2222-222222222222",
        "support agent",
    )

    prefix = (
        "https://ai.azure.com/nextgen/r/"
        "EREREREREREREREREREREQ,rg-agent-insights,,demo-account,demo-project"
    )
    assert project_url == (
        f"{prefix}/home?tid=22222222-2222-2222-2222-222222222222"
    )
    assert insights_url == (
        f"{prefix}/build/agents/support%20agent/monitor/insights?"
        "tid=22222222-2222-2222-2222-222222222222"
    )
