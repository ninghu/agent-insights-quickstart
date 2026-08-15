# Contributing

Contributions are welcome through GitHub issues and pull requests.

Before submitting a change:

1. Keep the skill portable and Azure-public-cloud scoped.
2. Do not add tenant IDs, subscription IDs, private endpoints, customer data, or
   credentials.
3. Preserve exact-scope, least-privilege, and ownership checks.
4. Add tests for changed behavior.
5. Run `pytest`, `ruff check .`, `mypy
   .agents/skills/agent-insights-onboarding/scripts`, Bicep validation, and Agent Skills
   validation.

Live Azure tests must use a disposable Agent Insights-enabled subscription. Scrub all
generated receipts before sharing logs or opening a pull request.
