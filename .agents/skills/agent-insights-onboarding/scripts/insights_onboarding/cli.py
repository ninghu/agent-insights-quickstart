"""Command-line interface for Agent Insights onboarding."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError

from .agents import project_client
from .azure_cli import AzureCli
from .discovery import (
    find_project_by_endpoint,
    list_app_insights_connections,
    list_application_insights,
    list_model_deployments,
    list_projects,
    list_recommended_insight_models,
    list_subscriptions,
    model_deployment_command,
    select_context,
)
from .errors import OnboardingError
from .models import OnboardingConfig
from .orchestrator import cleanup, doctor, onboard, status
from .validation import normalize_name


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _emit_progress(value: dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), file=sys.stderr, flush=True)


def _add_configuration(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=("scratch", "existing"), required=True)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--location")
    parser.add_argument("--agent-type", choices=("prompt", "hosted"))
    parser.add_argument("--name-prefix", default="agent-insights")
    parser.add_argument("--project-resource-id")
    parser.add_argument("--project-endpoint")
    parser.add_argument("--application-insights-resource-id")
    parser.add_argument("--agent-name")
    parser.add_argument("--model-deployment-name")
    parser.add_argument("--model-name", default="gpt-5.4")
    parser.add_argument("--model-version", default="2026-03-05")
    parser.add_argument("--model-format", default="OpenAI")
    parser.add_argument("--model-sku", default="GlobalStandard")
    parser.add_argument("--model-capacity", type=int, default=30)
    parser.add_argument("--lookback-hours", type=int, default=168)
    parser.add_argument("--create-sample-agent", action="store_true")
    parser.add_argument("--enable-existing-monitor", action="store_true")
    protection = parser.add_mutually_exclusive_group()
    protection.add_argument(
        "--protected-trace-content",
        dest="protected_trace_content",
        action="store_true",
        help="Require protected AppGenAIContent access.",
    )
    protection.add_argument(
        "--no-protected-trace-content",
        dest="protected_trace_content",
        action="store_false",
        help="Use normal Application Insights query access.",
    )
    parser.set_defaults(protected_trace_content=False)


def _config(args: argparse.Namespace) -> OnboardingConfig:
    if args.model_capacity <= 0:
        raise OnboardingError(
            "invalid_model_capacity",
            "Model capacity must be positive.",
        )
    if not 3 <= args.lookback_hours <= 2160:
        raise OnboardingError(
            "invalid_lookback",
            "Lookback hours must be between 3 and 2160.",
        )
    if args.mode == "scratch":
        if not args.location or not args.agent_type:
            raise OnboardingError(
                "incomplete_scratch_configuration",
                "Scratch mode requires --location and --agent-type.",
            )
        if args.create_sample_agent:
            raise OnboardingError(
                "invalid_agent_selection",
                "Scratch mode creates a sample Agent automatically.",
            )
    if args.mode == "existing":
        required = {
            "--agent-type": args.agent_type,
            "--project-resource-id": args.project_resource_id,
            "--model-deployment-name": args.model_deployment_name,
        }
        if not args.create_sample_agent:
            required["--agent-name"] = args.agent_name
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise OnboardingError(
                "incomplete_existing_configuration",
                "Existing mode is missing required arguments.",
                {"missing": missing},
            )
        if args.create_sample_agent and args.agent_name:
            raise OnboardingError(
                "conflicting_agent_selection",
                "Choose either a new sample Agent or an existing Agent, not both.",
            )
    return OnboardingConfig(
        mode=args.mode,
        subscription_id=args.subscription_id,
        location=args.location,
        agent_type=args.agent_type,
        name_prefix=args.name_prefix,
        project_resource_id=args.project_resource_id,
        project_endpoint=args.project_endpoint,
        application_insights_resource_id=args.application_insights_resource_id,
        agent_name=args.agent_name,
        model_deployment_name=args.model_deployment_name,
        model_name=args.model_name,
        model_version=args.model_version,
        model_format=args.model_format,
        model_sku=args.model_sku,
        model_capacity=args.model_capacity,
        lookback_hours=args.lookback_hours,
        create_sample_agent=args.create_sample_agent,
        invoke_existing_agent=False,
        enable_existing_monitor=args.enable_existing_monitor,
        protected_trace_content=args.protected_trace_content,
    )


def _doctor(args: argparse.Namespace) -> int:
    _emit(doctor(_config(args)))
    return 0


def _plan(args: argparse.Namespace) -> int:
    result = onboard(
        _config(args),
        run_id=args.run_id,
        dry_run=True,
    )
    _emit(result)
    return 0


def _onboard(args: argparse.Namespace) -> int:
    result = onboard(
        _config(args),
        run_id=args.run_id,
        dry_run=args.dry_run,
        ingestion_timeout_seconds=args.ingestion_timeout_seconds,
        insights_timeout_seconds=args.insights_timeout_seconds,
        progress_callback=_emit_progress,
    )
    _emit(result)
    return 0


def _status(args: argparse.Namespace) -> int:
    _emit(
        status(
            args.run_dir.resolve(),
            ingestion_timeout_seconds=args.ingestion_timeout_seconds,
            insights_timeout_seconds=args.insights_timeout_seconds,
        )
    )
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    _emit(cleanup(args.run_dir.resolve()))
    return 0


def _discover(args: argparse.Namespace) -> int:
    cli = AzureCli()
    if args.kind == "subscriptions":
        _emit({"status": "complete", "subscriptions": list_subscriptions(cli)})
        return 0
    if args.kind == "project":
        if not args.project_endpoint:
            raise OnboardingError(
                "missing_project_endpoint",
                "Project discovery requires --project-endpoint.",
            )
        _emit(
            {
                "status": "complete",
                "project": find_project_by_endpoint(cli, args.project_endpoint),
            }
        )
        return 0
    if not args.subscription_id:
        raise OnboardingError(
            "missing_subscription",
            "This discovery kind requires --subscription-id.",
        )
    context = select_context(cli, args.subscription_id)
    if args.kind == "projects":
        _emit(
            {
                "status": "complete",
                "azure_context": {
                    "subscription_id": context.subscription_id,
                    "subscription_name": context.subscription_name,
                    "tenant_id": context.tenant_id,
                },
                "projects": list_projects(cli, args.subscription_id),
            }
        )
        return 0
    if args.kind == "deployments":
        if not args.project_resource_id:
            raise OnboardingError(
                "missing_project_resource_id",
                "Deployment discovery requires --project-resource-id.",
            )
        _emit(
            {
                "status": "complete",
                "deployments": list_model_deployments(
                    cli,
                    args.project_resource_id,
                ),
            }
        )
        return 0
    if args.kind == "models":
        if not args.location:
            raise OnboardingError(
                "missing_location",
                "Model discovery requires --location.",
            )
        models = list_recommended_insight_models(
            cli,
            location=args.location,
            minimum_capacity=args.minimum_capacity,
        )
        if args.project_resource_id:
            for model in models:
                deployment_name = normalize_name(
                    f"agent-insights-{model['name']}",
                    max_length=64,
                )
                model["suggested_deployment_name"] = deployment_name
                model["deployment_command"] = model_deployment_command(
                    args.project_resource_id,
                    deployment_name=deployment_name,
                    model_name=str(model["name"]),
                    model_version=str(model["version"]),
                    model_format=str(model["format"]),
                    sku_name=str(model["sku_name"]),
                    capacity=int(model["recommended_capacity"]),
                )
        _emit({"status": "complete", "recommended_models": models})
        return 0
    if args.kind == "app-insights":
        _emit(
            {
                "status": "complete",
                "application_insights": list_application_insights(
                    cli,
                    args.subscription_id,
                    resource_group=args.resource_group,
                ),
            }
        )
        return 0
    if args.kind == "connections":
        if not args.project_resource_id:
            raise OnboardingError(
                "missing_project_resource_id",
                "Connection discovery requires --project-resource-id.",
            )
        _emit(
            {
                "status": "complete",
                "application_insights_connections": list_app_insights_connections(
                    cli,
                    args.project_resource_id,
                ),
            }
        )
        return 0
    if not args.project_endpoint:
        raise OnboardingError(
            "missing_project_endpoint",
            "Agent discovery requires --project-endpoint.",
        )
    project = project_client(args.project_endpoint, context.tenant_id)
    agents = []
    try:
        for item in project.agents.list(limit=100, order="asc"):
            agents.append(
                {
                    "name": str(getattr(item, "name", "") or ""),
                    "description": str(getattr(item, "description", "") or ""),
                }
            )
    except HttpResponseError as error:
        raise OnboardingError(
            "agent_discovery_failed",
            "Foundry rejected Agent discovery for the selected project endpoint.",
            {"status": error.status_code},
        ) from error
    except (ServiceRequestError, ServiceResponseError) as error:
        raise OnboardingError(
            "agent_discovery_unavailable",
            "Foundry Agent discovery could not reach the selected project endpoint.",
        ) from error
    _emit({"status": "complete", "agents": agents})
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure and validate Microsoft Foundry Agent Insights.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser(
        "discover",
        help="Read-only discovery for guided resource selection.",
    )
    discover_parser.add_argument(
        "kind",
        choices=(
            "subscriptions",
            "project",
            "projects",
            "connections",
            "app-insights",
            "deployments",
            "models",
            "agents",
        ),
    )
    discover_parser.add_argument("--subscription-id")
    discover_parser.add_argument("--project-endpoint")
    discover_parser.add_argument("--project-resource-id")
    discover_parser.add_argument("--resource-group")
    discover_parser.add_argument("--location")
    discover_parser.add_argument("--minimum-capacity", type=int, default=30)
    discover_parser.set_defaults(handler=_discover)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run read-only prerequisites and permission checks.",
    )
    _add_configuration(doctor_parser)
    doctor_parser.set_defaults(handler=_doctor)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Persist the exact mutation plan without Azure writes.",
    )
    _add_configuration(plan_parser)
    plan_parser.add_argument("--run-id")
    plan_parser.set_defaults(handler=_plan)

    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Plan, apply, and verify Agent Insights onboarding.",
    )
    _add_configuration(onboard_parser)
    onboard_parser.add_argument("--run-id")
    onboard_parser.add_argument("--dry-run", action="store_true")
    onboard_parser.add_argument(
        "--ingestion-timeout-seconds",
        type=float,
        default=900,
    )
    onboard_parser.add_argument(
        "--insights-timeout-seconds",
        type=float,
        default=21600,
    )
    onboard_parser.set_defaults(handler=_onboard)

    status_parser = subparsers.add_parser(
        "status",
        help="Resume ingestion and Agent Insights polling without replaying traffic.",
    )
    status_parser.add_argument("--run-dir", type=Path, required=True)
    status_parser.add_argument(
        "--ingestion-timeout-seconds",
        type=float,
        default=900,
    )
    status_parser.add_argument(
        "--insights-timeout-seconds",
        type=float,
        default=21600,
    )
    status_parser.set_defaults(handler=_status)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Remove only resources recorded as created by a completed run.",
    )
    cleanup_parser.add_argument("--run-dir", type=Path, required=True)
    cleanup_parser.set_defaults(handler=_cleanup)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for name in ("ingestion_timeout_seconds", "insights_timeout_seconds"):
        value = getattr(args, name, 1)
        if value <= 0:
            raise OnboardingError(
                "invalid_timeout",
                f"{name.replace('_', ' ')} must be positive.",
            )
    return int(args.handler(args))
