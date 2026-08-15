#!/usr/bin/env python3
"""Portable entry point for the Agent Insights onboarding skill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def main() -> int:
    try:
        import httpx
        import openai
        from azure.core.exceptions import (
            HttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
        )
        from insights_onboarding.cli import main as cli_main
        from insights_onboarding.errors import OnboardingError
        from insights_onboarding.receipts import ensure_secret_free
    except ModuleNotFoundError as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "code": "dependency_missing",
                        "message": (
                            "Install the repository's pinned Python dependencies before "
                            "running onboarding."
                        ),
                        "details": {"module": error.name},
                    },
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        return cli_main()
    except OnboardingError as error:
        payload = error.as_dict()
        ensure_secret_free(payload)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return error.exit_code
    except HttpResponseError as error:
        payload = {
            "status": "failed",
            "error": {
                "code": "azure_sdk_request_failed",
                "message": "Azure or Foundry rejected a requested operation.",
                "details": {"status": error.status_code},
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    except (ServiceRequestError, ServiceResponseError, httpx.RequestError):
        payload = {
            "status": "failed",
            "error": {
                "code": "azure_service_unavailable",
                "message": "An Azure or Foundry endpoint could not be reached.",
                "details": {},
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    except openai.APIError as error:
        status = getattr(error, "status_code", None)
        payload = {
            "status": "failed",
            "error": {
                "code": "agent_invocation_failed",
                "message": "Foundry rejected an Agent invocation.",
                "details": {"status": status} if isinstance(status, int) else {},
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
