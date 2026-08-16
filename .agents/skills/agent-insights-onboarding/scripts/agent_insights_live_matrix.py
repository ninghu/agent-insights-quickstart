#!/usr/bin/env python3
"""Portable entry point for the disposable Azure onboarding matrix."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def main() -> int:
    from insights_onboarding.errors import OnboardingError
    from insights_onboarding.live_matrix import main as matrix_main
    from insights_onboarding.receipts import ensure_secret_free

    try:
        return matrix_main()
    except OnboardingError as error:
        payload = error.as_dict()
        ensure_secret_free(payload)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
