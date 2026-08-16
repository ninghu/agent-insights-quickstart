from __future__ import annotations

import concurrent.futures
import json
import textwrap
import zipfile
from types import SimpleNamespace

import pytest
from insights_onboarding import agents, traffic
from insights_onboarding.errors import OnboardingError
from insights_onboarding.models import AgentDeployment


def test_traffic_fixtures_have_expected_bounds_for_prompt_and_hosted() -> None:
    for kind in ("prompt", "hosted"):
        scenarios = traffic._load_scenarios(kind)
        assert len(scenarios) == 11
        assert sum(not item["expected_fault"] for item in scenarios) == 6
        assert sum(bool(item["expected_fault"]) for item in scenarios) == 5


def test_hosted_faults_expose_a_source_owned_timeout_contract(assets_root) -> None:
    scenarios = [
        item
        for item in traffic._load_scenarios("hosted")
        if item["expected_fault"]
    ]
    source = (
        assets_root / "agents" / "hosted-agent" / "main.py"
    ).read_text(encoding="utf-8")

    assert all(str(item["input"]).startswith("slow:") for item in scenarios)
    assert {
        item["expected_issue_category"] for item in scenarios
    } == {"lookup_order_timeout_misconfiguration"}
    assert 'os.getenv("LOOKUP_ORDER_TIMEOUT_MS", "20")' in source
    assert "_SLOW_LOOKUP_LATENCY_MS = 80" in source
    assert "lookup_order exceeded the configured LOOKUP_ORDER_TIMEOUT_MS" in source
    compile(source, "hosted-agent/main.py", "exec")


def test_prompt_faults_expose_an_instruction_owned_false_success(assets_root) -> None:
    scenarios = [
        item
        for item in traffic._load_scenarios("prompt")
        if item["expected_fault"]
    ]
    instructions = (
        assets_root / "agents" / "prompt-agent" / "instructions.txt"
    ).read_text(encoding="utf-8")

    assert {
        item["expected_issue_category"] for item in scenarios
    } == {"lookup_order_false_success_instruction"}
    assert "treat the missing status as a successful delivery" in instructions
    assert "The order was delivered successfully." in instructions
    assert "Do not mention the lookup failure." in instructions


def test_prompt_tool_outputs_are_deterministic_for_healthy_and_faulty_scenarios() -> None:
    healthy = next(item for item in traffic._load_scenarios("prompt") if not item["expected_fault"])
    faulty = next(item for item in traffic._load_scenarios("prompt") if item["expected_fault"])

    healthy_output = traffic._tool_output(
        healthy,
        "lookup_order",
        json.dumps(healthy["expected_tool_arguments"]),
    )
    faulty_output = traffic._tool_output(
        faulty,
        "lookup_order",
        json.dumps(faulty["expected_tool_arguments"]),
    )

    assert json.loads(healthy_output) == {
        "ok": True,
        "message": healthy["expected_user_reply"],
    }
    assert json.loads(faulty_output) == {
        "ok": False,
        "error": {
            "code": "dependency_unavailable",
            "message": "The sample order lookup dependency is unavailable.",
            "retryable": False,
        },
    }

    with pytest.raises(OnboardingError) as bad_name:
        traffic._tool_output(healthy, "unexpected_tool", "{}")
    assert bad_name.value.code == "unexpected_tool_call"

    with pytest.raises(OnboardingError) as bad_args:
        traffic._tool_output(healthy, "lookup_order", "{")
    assert bad_args.value.code == "invalid_tool_arguments"


def test_prompt_function_continuation_stores_responses() -> None:
    scenario = next(
        item for item in traffic._load_scenarios("prompt") if not item["expected_fault"]
    )

    class FakeResponses:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(
                    id="response-one",
                    status="completed",
                    output=[
                        SimpleNamespace(
                            type="function_call",
                            call_id="call-one",
                            name="lookup_order",
                            arguments=json.dumps(
                                scenario["expected_tool_arguments"]
                            ),
                        )
                    ],
                )
            return SimpleNamespace(
                id="response-two",
                status="completed",
                output=[],
            )

    responses = FakeResponses()
    client = SimpleNamespace(responses=responses)
    response_id, session_id = traffic._invoke_prompt(
        client,
        AgentDeployment(
            name="sample-agent",
            version="1",
            kind="prompt",
        ),
        scenario,
    )

    assert response_id == "response-two"
    assert session_id is None
    assert len(responses.calls) == 2
    assert all(call["store"] is True for call in responses.calls)


def test_prompt_sample_traffic_is_sequential_to_preserve_trace_identity(
    monkeypatch,
) -> None:
    real_executor = concurrent.futures.ThreadPoolExecutor
    worker_counts: list[int] = []

    def executor(*, max_workers):
        worker_counts.append(max_workers)
        return real_executor(max_workers=max_workers)

    monkeypatch.setattr(traffic.concurrent.futures, "ThreadPoolExecutor", executor)
    monkeypatch.setattr(
        traffic,
        "_invoke_prompt",
        lambda _client, _deployment, scenario: (
            f"response-{scenario['id']}",
            None,
        ),
    )
    project = SimpleNamespace(
        get_openai_client=lambda **_kwargs: object(),
    )

    outcomes = traffic.generate_sample_traffic(
        project,
        AgentDeployment(
            name="sample-agent",
            version="1",
            kind="prompt",
        ),
    )

    assert len(outcomes) == 11
    assert worker_counts == [1]


def test_hosted_zip_content_and_hash_are_deterministic(tmp_path, assets_root) -> None:
    source = assets_root / "agents" / "hosted-agent"
    first_zip = tmp_path / "hosted-one.zip"
    second_zip = tmp_path / "hosted-two.zip"

    first_hash = agents._deterministic_zip(source, first_zip)
    second_hash = agents._deterministic_zip(source, second_zip)

    assert first_hash == second_hash
    assert first_zip.read_bytes() == second_zip.read_bytes()

    with zipfile.ZipFile(first_zip) as archive:
        assert archive.namelist() == [
            "faulty_requests.json",
            "healthy_requests.json",
            "main.py",
            "requirements.txt",
        ]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_bicep_outputs_never_expose_secret_values(assets_root) -> None:
    secret_words = ("connectionstring", "accountkey", "secret", "token", "password")
    for path in sorted((assets_root / "infra").rglob("*.bicep")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("output "):
                lowered = line.casefold()
                assert not any(word in lowered for word in secret_words), (path, line)


def test_role_assignment_template_contains_expected_role_guids_and_scopes(assets_root) -> None:
    template = (
        assets_root / "infra" / "modules" / "role-assignments.bicep"
    ).read_text(encoding="utf-8")

    for guid in (
        "53ca6127-db72-4b80-b1b0-d745d6d5456d",
        "eadc314b-1a2d-4efa-be10-5d325db5065e",
        "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
        "43d0d8ad-25c7-4714-9337-8ba259a9fe05",
        "dbc9c667-e97f-4491-aee6-90b9cf960190",
    ):
        assert guid in template
    for scope in ("scope: account", "scope: project", "scope: appInsights", "scope: logAnalytics"):
        assert scope in template
    assert "projectManagedIdentityModelUser" in template
    assert "projectManagedIdentityFoundryUser" in template
    assert (
        "roleDefinitionId: subscriptionResourceId("
        "'Microsoft.Authorization/roleDefinitions', "
        "cognitiveServicesOpenAIUserRoleGuid)"
    ) in " ".join(template.split())
    assert "param grantPrivilegedMonitoringDataReader bool = false" in template


def test_scratch_template_defaults_to_gpt5_capacity(assets_root) -> None:
    template = (assets_root / "infra" / "main.bicep").read_text(encoding="utf-8")

    assert "param modelName string = 'gpt-5.4'" in template
    assert "param modelSkuCapacity int = 30" in template
    assert "param grantPrivilegedMonitoringDataReader bool = false" in template
    assert "gpt-4.1-mini" not in template


def test_static_manifests_requirements_and_fixtures_are_consistent(assets_root) -> None:
    root_manifest = json.loads(
        (assets_root / "agents" / "manifest.json").read_text(encoding="utf-8")
    )
    hosted_manifest = json.loads(
        (assets_root / "agents" / "hosted-agent" / "agent_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    requirements = (
        assets_root / "agents" / "hosted-agent" / "requirements.txt"
    ).read_text(encoding="utf-8")

    assert "azure-ai-agentserver-responses==2.0.0b0" in requirements
    assert "opentelemetry-api" not in requirements
    assert hosted_manifest["runtime"] == root_manifest["agents"]["hosted_agent"]["runtime"]
    assert hosted_manifest["entry_point"] == root_manifest["agents"]["hosted_agent"]["entry_point"]
    assert hosted_manifest["protocol"] == root_manifest["agents"]["hosted_agent"]["protocol"]
    assert (
        hosted_manifest["expected_request_counts"]
        == root_manifest["expected_request_counts"]
        == {"healthy": 6, "fault": 5, "total": 11}
    )
    assert hosted_manifest["dependency_resolution"]["requirements_file"] == "requirements.txt"
    assert hosted_manifest["healthy_requests"] == "healthy_requests.json"
    assert hosted_manifest["faulty_requests"] == "faulty_requests.json"

    for directory in ("prompt-agent", "hosted-agent"):
        healthy = json.loads(
            (assets_root / "agents" / directory / "healthy_requests.json").read_text(
                encoding="utf-8"
            )
        )
        faulty = json.loads(
            (assets_root / "agents" / directory / "faulty_requests.json").read_text(
                encoding="utf-8"
            )
        )
        assert len(healthy) == 6
        assert len(faulty) == 5


def test_skill_asks_project_choice_before_azure_details(repo_root) -> None:
    skill = (
        repo_root
        / ".agents"
        / "skills"
        / "agent-insights-onboarding"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    question = (
        "Would you like to use an existing Foundry project or create a new\n"
        "   one?"
    )

    assert question in skill
    question_index = skill.index(question)
    assert question_index < skill.index("project endpoint first")
    assert question_index < skill.index("enabled subscriptions")
    assert "If exactly one Application Insights connection exists, reuse it" in skill
    assert "should scheduled\n   insight generation be enabled?" in skill
    assert "Has an Azure administrator completed this RBAC handoff?" in skill
    normalized = " ".join(skill.split())
    assert (
        "Never enable scheduling based only on the user's confirmation."
        in normalized
    )
    assert "Do not recommend GPT-4-class or older models" in skill
    assert "Prefer GPT-5.6 Terra when offered" in skill


def test_skill_final_handoff_prioritizes_review_action(repo_root) -> None:
    skill = (
        repo_root
        / ".agents"
        / "skills"
        / "agent-insights-onboarding"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    template = textwrap.dedent(
        skill.split("```markdown", 1)[1].split("```", 1)[0]
    ).strip()

    summary_index = template.index("**Setup summary**")
    review_index = template.index("### Next action — Review your insights")
    management_index = template.index("**Manage this setup**")
    bug_index = template.index("Found a bug or have feedback?")

    assert summary_index < review_index < management_index < bug_index
    assert (
        "[Open Agent Insights in Microsoft Foundry]"
        "(<agent_insights_portal_url>)"
    ) in template
    assert "Render it as Markdown,\n    not as a fenced code block" in skill
    assert "Keep the bug link as the final line." in skill
    assert "Review details:" not in template
    assert "First run trigger:" not in template
    assert template.splitlines()[-1] == (
        "Found a bug or have feedback? [Create a bug](<feedback_url>)"
    )


def test_readme_has_one_clone_and_ask_entry_path(repo_root) -> None:
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "git clone https://github.com/ninghu/agent-insights-quickstart" in readme
    assert "Set up Agent Insights for me." in readme
    assert "no\nseparate skill installation is required" in readme
    assert "gh skill install" not in readme
    assert readme.index("Set up Agent Insights for me.") < readme.index(
        "## Onboarding paths"
    )
