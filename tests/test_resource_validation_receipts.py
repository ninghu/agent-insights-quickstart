from __future__ import annotations

import json

import pytest
from insights_onboarding.errors import OnboardingError
from insights_onboarding.models import OnboardingPlan
from insights_onboarding.receipts import ensure_secret_free, read_json, write_json_atomic
from insights_onboarding.resource_ids import (
    normalize_resource_id,
    parse_resource_id,
    require_resource_type,
)
from insights_onboarding.validation import (
    normalize_name,
    require_owned_tags,
    validate_plan_context,
    validate_project_endpoint,
    validate_run_id,
)


def test_parse_resource_id_nested_type_name_and_parent(azure_ids: dict[str, str]) -> None:
    parsed = parse_resource_id(azure_ids["project"])

    assert parsed.subscription_id == "11111111-1111-1111-1111-111111111111"
    assert parsed.resource_group == "rg-agent-insights"
    assert parsed.namespace == "Microsoft.CognitiveServices"
    assert parsed.types == ("accounts", "projects")
    assert parsed.names == ("demo-account", "demo-project")
    assert parsed.resource_type == "Microsoft.CognitiveServices/accounts/projects"
    assert parsed.name == "demo-project"
    assert parsed.resource_group_id.endswith("/resourceGroups/rg-agent-insights")
    assert parsed.parent().raw == azure_ids["account"]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("subscriptions/no-leading-slash", "invalid_resource_id"),
        (
            "/subscriptions/not-a-guid/resourceGroups/rg/providers/Microsoft.Insights/components/x",
            "invalid_subscription_id",
        ),
        (
            "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/"
            "providers/Microsoft.Insights/components",
            "invalid_resource_id",
        ),
    ],
)
def test_invalid_resource_ids_raise_expected_codes(value: str, code: str) -> None:
    with pytest.raises(OnboardingError) as excinfo:
        parse_resource_id(value)

    assert excinfo.value.code == code


def test_parent_and_expected_type_validation_errors(azure_ids: dict[str, str]) -> None:
    with pytest.raises(OnboardingError) as excinfo:
        parse_resource_id(azure_ids["account"]).parent()

    assert excinfo.value.code == "invalid_resource_parent"

    with pytest.raises(OnboardingError) as mismatch:
        require_resource_type(azure_ids["app_insights"], "Microsoft.CognitiveServices/accounts")

    assert mismatch.value.code == "unexpected_resource_type"
    assert normalize_resource_id(f"  {azure_ids['project']}/ ") == azure_ids["project"]


def test_name_endpoint_and_run_id_validation() -> None:
    assert normalize_name(" Agent Insights! Demo ") == "agent-insights-demo"
    assert validate_run_id("abc123def456") == "abc123def456"
    assert (
        validate_project_endpoint(
            "https://demo.services.ai.azure.com/api/projects/demo%20project/",
            "Demo Project",
            "demo",
        )
        == "https://demo.services.ai.azure.com/api/projects/demo%20project"
    )

    with pytest.raises(OnboardingError, match="invalid format"):
        validate_run_id("too-short")

    for value in (
        "http://demo.services.ai.azure.com/api/projects/demo",
        "https://demo.contoso.com/api/projects/demo",
        "https://demo.services.ai.azure.com/api/projects/demo?x=1",
        "https://demo.services.ai.azure.com/projects/demo",
    ):
        with pytest.raises(OnboardingError) as excinfo:
            validate_project_endpoint(value)
        assert excinfo.value.code == "invalid_project_endpoint"

    with pytest.raises(OnboardingError) as account_mismatch:
        validate_project_endpoint(
            "https://other.services.ai.azure.com/api/projects/demo",
            "demo",
            "expected",
        )
    assert account_mismatch.value.code == "project_endpoint_mismatch"


def test_ownership_and_plan_context_validation(
    make_config, azure_context, run_id: str
) -> None:
    require_owned_tags(
        {
            "Created-By": "agent-insights-quickstart",
            "RUN-ID": run_id,
            "owner-object-id": azure_context.user_object_id,
        },
        run_id=run_id,
        owner_object_id=azure_context.user_object_id,
    )

    with pytest.raises(OnboardingError) as ownership:
        require_owned_tags(
            {"created-by": "someone-else", "run-id": run_id},
            run_id=run_id,
            owner_object_id=azure_context.user_object_id,
        )

    assert ownership.value.code == "ownership_mismatch"
    assert ownership.value.details["mismatches"]["created-by"]["expected"] == (
        "agent-insights-quickstart"
    )

    plan = OnboardingPlan(
        schema_version=1,
        run_id=run_id,
        created_at="2026-01-02T03:04:05+00:00",
        mode="scratch",
        config={},
        azure_context={
            "subscription_id": azure_context.subscription_id,
            "tenant_id": azure_context.tenant_id,
            "user_object_id": azure_context.user_object_id,
        },
        mutations=(),
        expected={"traffic": {"total": 11}},
        plan_hash="ignored-for-validation",
    )

    with pytest.raises(OnboardingError) as changed:
        validate_plan_context(
            plan,
            subscription_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            tenant_id=azure_context.tenant_id,
            user_object_id=azure_context.user_object_id,
        )

    assert changed.value.code == "plan_context_changed"
    assert changed.value.details["mismatches"]["subscription_id"]["planned"] == (
        azure_context.subscription_id
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"apiKey": "safe-looking"},
        {"nested": {"Authorization": "value"}},
        {"message": "Bearer abcdefghijklmnopqrstuvwx"},
        {"message": "InstrumentationKey=super-secret"},
    ],
)
def test_receipt_secret_detection_rejects_forbidden_keys_and_values(payload: object) -> None:
    with pytest.raises(OnboardingError) as excinfo:
        ensure_secret_free(payload)

    assert excinfo.value.code == "secret_in_receipt"


def test_atomic_receipt_round_trip_and_invalid_shapes(tmp_path) -> None:
    receipt = tmp_path / "receipts" / "final.json"
    payload = {
        "status": "complete",
        "steps": [{"name": "plan"}, {"name": "verify"}],
        "metadata": {"owner": "agent-insights-quickstart"},
    }

    write_json_atomic(receipt, payload)

    assert receipt.read_text(encoding="utf-8").endswith("\n")
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload
    assert read_json(receipt) == payload
    assert list(receipt.parent.glob(".final.json.*.tmp")) == []

    receipt.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(OnboardingError) as invalid:
        read_json(receipt)

    assert invalid.value.code == "invalid_receipt"
