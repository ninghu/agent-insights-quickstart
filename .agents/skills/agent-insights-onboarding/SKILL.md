---
name: agent-insights-onboarding
description: Configure and validate Microsoft Foundry Agent Insights on an existing project or a new scratch environment. Use when asked to enable, set up, try, onboard, diagnose permissions for, or generate a first result with Agent Insights, including Prompt Agent and source-code Hosted Agent quickstarts.
license: MIT
---

# Agent Insights Onboarding

Run the reviewed onboarding CLI; do not compose Azure mutations independently.

## Safety contract

- Support Azure public cloud only.
- Never guess a tenant, subscription, project, agent, model, or Application Insights resource.
- Never grant Owner or broaden a role assignment above the exact documented resource.
- Never delete or replace a pre-existing agent, version, monitor, connection, or role assignment.
- Never invoke an existing customer agent unless the user separately opts in after seeing the agent name and traffic bound.
- Never print or persist access tokens, connection strings, keys, authorization headers, or raw customer telemetry.
- Treat partial ingestion, a failed Agent Insights run, or an empty first result as failure.
- If traffic was already generated, resume with `status`; never replay the same run.

Read [permissions](references/permissions.md) before applying RBAC. Read the workflow
reference for the selected path:

- [existing resources](references/existing-resources.md)
- [scratch environment](references/scratch-environment.md)

## Guided workflow

1. Ask the user to choose **Existing Foundry project** or **New scratch environment**.
2. For scratch, ask the user to choose **Prompt Agent** or **Code-based Hosted Agent**.
3. Check for Python 3.13+, Azure CLI 2.80+, and Git. If a tool is missing, ask before
   installing it and use only the vendor's documented installer.
4. Create an ignored `.venv` with Python 3.13 when needed and install the reviewed
   `scripts/requirements.txt` with `python -m pip`. Use that environment's Python for
   every command below. Do not install into the system interpreter.
5. Require an interactive Azure CLI user session. If necessary, run `az login`, then
   show the available subscriptions and ask the user to select one. Do not use a fixed
   tenant or subscription.
6. Gather only the choices needed by the selected path. Prefer CLI discovery over
   asking the user to paste resource IDs.
7. From the repository root, run the read-only doctor first:

   ```text
   python .agents/skills/agent-insights-onboarding/scripts/agent_insights_onboard.py doctor <arguments>
   ```

8. Show the doctor's non-secret context and exact missing prerequisites. Stop before
   mutation if the subscription is not Agent Insights-enabled, the cloud is not
   `AzureCloud`, permissions are insufficient, the model lacks quota, or resources are
   ambiguous.
9. When doctor returns `ready`, run:

   ```text
   python .agents/skills/agent-insights-onboarding/scripts/agent_insights_onboard.py onboard <same arguments>
   ```

   The CLI freezes and prints a plan, then automatically applies it. Do not insert a
   second approval prompt for the planned RBAC writes.
10. Report progress from the CLI's JSON events without exposing subprocess output that
   the CLI redacted.
11. Require a final receipt with `status: complete`. Give the user the Foundry portal
    link, agent/version, monitor/run/insight IDs, cost estimate when returned by the
    service, receipt path, and cleanup command.

## Recovery

- Use the run directory printed by the CLI.
- If provisioning stopped before traffic, rerun `onboard` with the same arguments; only
  exact receipt-owned resources may be reused.
- If a traffic receipt exists, run `status --run-dir <path>`.
- Do not generate replacement traffic to compensate for delayed or missing ingestion.
- For categorized failures and administrator handoff guidance, read
  [troubleshooting](references/troubleshooting.md).

## Skill development

For changes to this skill, use `doctor` or `onboard --dry-run`. Do not create Azure
resources or traffic merely to inspect the skill. Live acceptance requires an explicitly
selected disposable, Agent Insights-enabled subscription.
