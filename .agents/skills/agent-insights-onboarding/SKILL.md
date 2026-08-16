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
- Never invoke an existing customer Agent. Only a sample Agent created by the workflow
  may receive the reviewed bounded traffic fixtures.
- Never print or persist access tokens, connection strings, keys, authorization headers, or raw customer telemetry.
- Treat partial ingestion, a failed Agent Insights run, or an empty first result as failure.
- If traffic was already generated, resume with `status`; never replay the same run.

Read [permissions](references/permissions.md) before applying RBAC. Read the workflow
reference for the selected path:

- [existing resources](references/existing-resources.md)
- [scratch environment](references/scratch-environment.md)
- [insight generation model](references/model-selection.md)

## Guided workflow

1. Ask this first question before requesting an endpoint, subscription, or any other
   Azure value: **Would you like to use an existing Foundry project or create a new
   one?** Offer exactly these choices:
   - **Use an existing Foundry project (Recommended)**
   - **Create a new Foundry project**
2. Check for Python 3.13+, Azure CLI 2.80+, and Git. If a tool is missing, ask before
   installing it and use only the vendor's documented installer.
3. Treat the directory containing this `SKILL.md` as `<skill-root>`. Create an ignored
   `.venv` with Python 3.13 when needed and install the reviewed
   `<skill-root>/scripts/requirements.txt` with `python -m pip`. Use that environment's
   Python for every command below. Do not install into the system interpreter.
4. Require an interactive Azure CLI user session. If necessary, run `az login`. Do not
   use a fixed tenant or subscription.
5. For an existing project, ask for its Foundry project endpoint first. Run
   `discover project --project-endpoint <endpoint>` to resolve its subscription and ARM
   resource ID across enabled subscriptions in the active tenant. Ask for a subscription
   only if the endpoint cannot be resolved or is ambiguous.
6. For an existing project, run `discover connections` with the resolved subscription
   and project resource ID:
   - If exactly one Application Insights connection exists, reuse it without asking the
     user to choose another component.
   - If none exists, run `discover app-insights` in the project's resource group first,
     then subscription-wide if needed. Ask the user to select a component; the onboarding
     CLI creates the missing project/account connection and scoped access.
   - If multiple Application Insights connections exist, stop with the ambiguity instead
     of guessing or deleting one.
7. For an existing project, ask: **Would you like to use an existing Agent or create a
   new sample Agent in this project?** Offer:
   - **Create a new sample Agent (Recommended)**
   - **Use an existing Agent**

   For a new sample Agent, ask for **Prompt Agent** or **Code-based Hosted Agent** and pass
   `--create-sample-agent`. Do not ask for `--agent-name` or
   any traffic opt-in; the CLI creates a deterministic receipt-owned Agent and
   sends the bounded six healthy plus five faulty sample requests. For an existing Agent,
   run `discover agents` and ask the user to select one. Doctor checks for at least three
   recent correlated traces without invoking the Agent. If none exist, tell the user to
   run their normal application or test traffic, wait for Application Insights ingestion,
   and rerun the same doctor command.
8. For an existing project, ask: **After the first insight result, should scheduled
   insight generation be enabled?** Offer:
   - **Yes, enable scheduled insights (Recommended)**
   - **No, keep this as a one-off**

   Pass `--enable-existing-monitor` only when the user selects yes. A new monitor uses a
   24-hour interval; an existing monitor keeps its current interval. If the monitor is
   already enabled, preserve it and report its next scheduled run.
   Enabling a disabled monitor schedules an immediate first occurrence. Use that
   scheduled run for first-result verification; never create an additional manual run.
   Create a manual run only for one-off onboarding.
   One-off runs use caller OBO. Do not plan Project MI model, project, or monitoring
   roles unless scheduled generation is enabled.
9. Select the insight generation model:
   - For an existing project, run `discover deployments` and recommend a current GPT-5+
     deployment.
   - If none exists, or for a new project, run `discover models` for the selected region.
     Prefer GPT-5.6 Terra when offered; otherwise use an available current GPT-5+ model.
   - If deployment is required, show the returned exact command and ask the user to
     confirm. Do not overwrite a different existing deployment. If the caller lacks
     permission, hand the same command to an Azure administrator and verify afterward.
   - Do not recommend GPT-4-class or older models for production insights.
10. When creating a new project, ask the user to choose **Prompt Agent** or
   **Code-based Hosted Agent**, then show enabled subscriptions and ask them to select
   one.
11. Gather only the choices needed by the selected path. Prefer CLI discovery over
   asking the user to paste resource IDs.
12. From the user's current repository root, run the read-only doctor first:

   ```text
   python "<skill-root>/scripts/agent_insights_onboard.py" doctor <arguments>
   ```

13. Show the doctor's non-secret context and exact missing prerequisites. Stop before
   mutation if the subscription is not Agent Insights-enabled, the cloud is not
   `AzureCloud`, permissions are insufficient, the model lacks quota, or resources are
   ambiguous.
14. If doctor returns `insufficient_preflight_permission` with `admin_handoff`, show the
   exact principal, role, scope, and command list. Ask:
   **Has an Azure administrator completed this RBAC handoff?**
   - **Yes, recheck access**
   - **No, stop and keep the handoff**

   On yes, rerun the same doctor command and require `status: ready`. Never enable
   scheduling based only on the user's confirmation.
15. When doctor returns `ready`, run:

   ```text
   python "<skill-root>/scripts/agent_insights_onboard.py" onboard <same arguments>
   ```

   The CLI freezes and prints a plan, then automatically applies it. Do not insert a
   second approval prompt for the planned RBAC writes.
16. When the CLI emits `status: insights_running`, immediately give the user its
   `agent_insights_portal_url` and ask them to open it. Explain that the first run may
   take 10–20 minutes. Keep monitoring the command and continue the workflow; do not
   make the user wait without the portal link. Report other progress without exposing
   subprocess output that the CLI redacted.
17. Require a final receipt with `status: complete`. For a code-based Hosted sample,
    also require `result_summary.concrete_code_fix_count >= 1`. For a Prompt sample,
    require `result_summary.concrete_prompt_fix_count >= 1`, grounded to the system
    instructions surface. Prose-only output is a demo regression, not success. Give the
    user the first-result insight count, applicable concrete-fix count, agent/version,
    cost estimate when returned by the service, schedule interval/next run when enabled,
    receipt path, cleanup command, and Foundry portal link. Keep low-level monitor,
    run, and insight IDs in the receipt instead of duplicating them in chat.

    Make the final response consist only of the handoff below. Render it as Markdown,
    not as a fenced code block. Do not prefix it with another completion sentence,
    repeat `status: complete`, show the raw portal URL, or repeat the first-run trigger.
    Omit optional rows whose values were not returned. Show only the Agent's applicable
    concrete-fix type; do not show an unrelated zero-count code or prompt-fix row.
    Keep the bug link as the final line.

    ```markdown
    **Agent Insights setup complete.**

    **Setup summary**
    - **Agent:** `<agent_name>` (`<agent_version>`, `<agent_kind>`)
    - **Insights generated:** `<insight_count>`
    - **Concrete fixes:** `<concrete_fix_count> <code|system prompt> fixes`
    - **Schedule:** `<Enabled — every schedule_interval | Not enabled — one-off run>`
    - **Next run:** `<next_run>` (scheduled runs only)
    - **Estimated cost:** `<estimated_cost>` (when returned)

    ### Next action — Review your insights

    [Open Agent Insights in Microsoft Foundry](<agent_insights_portal_url>)

    Review the generated insights and any concrete fixes. If the portal opens the
    project home, select **Monitor > Agent Insights**.

    **Manage this setup**
    - **Receipt:** `<receipt_path>`
    - **Cleanup:** `<cleanup_command>`

    Found a bug or have feedback? [Create a bug](<feedback_url>)
    ```

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

Run `pytest tests/test_path_matrix.py` from the repository root for the complete offline
decision matrix. Before a release that changes orchestration, run
`.agents/skills/agent-insights-onboarding/scripts/agent_insights_live_matrix.py
--confirm-live` with an interactive Azure user in an explicitly selected disposable
subscription; never run the live matrix against customer resources.
