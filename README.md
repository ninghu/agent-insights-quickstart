# Agent Insights Quickstart

Set up and try Microsoft Foundry Agent Insights with a guided Agent Skill. Use an
existing Foundry project or create a self-contained scratch environment, then validate
the complete path from permissions and telemetry to a visible first insight.

> [!IMPORTANT]
> This is a community preview sample, not an official Microsoft support channel.
> Agent Insights must already be enabled for the target subscription. The initial
> release supports Azure public cloud only.

## Quick start

Clone the repository:

```shell
git clone https://github.com/ninghu/agent-insights-quickstart
cd agent-insights-quickstart
```

Open the folder in **GitHub Copilot**, **Claude Code**, **Codex**, or another
[Agent Skills](https://agentskills.io)-compatible agent, then ask:

```text
Set up Agent Insights for me.
```

That is all. The repository already contains the skill under `.agents/skills`; no
separate skill installation is required. Your agent checks Python and Azure CLI,
guides Azure sign-in, discovers available resources, and runs the read-only doctor
before making changes.

The first question is whether to **use an existing Foundry project** or **create a new
Foundry project**. No subscription or endpoint is requested before that choice.

You still need an Agent Insights-enabled Azure subscription and permission to manage
the selected resources. The workflow reports any missing access before mutation.

## Onboarding paths

| Path | Result |
| --- | --- |
| Existing Foundry project | Resolves the endpoint, validates or creates the Application Insights connection, repairs missing scoped access, validates an Agent Insights result, and asks whether to enable scheduled generation. |
| Create a new Foundry project | Creates a tagged scratch project and monitoring stack, deploys either a Prompt Agent or source-code Hosted Agent, sends bounded sample traffic, enables Agent Insights, and verifies a first insight. |

For best insight quality, the workflow recommends a current **GPT-5+** model. If the
project has no suitable deployment, it finds quota-backed candidates and helps deploy
one after confirmation.

The scratch environment stays available after success. The final receipt identifies
the number of insights returned, includes a direct Foundry agent-monitor link for
reviewing details, identifies cost-bearing resources, and prints an ownership-checked
cleanup command.

## What the workflow changes

The workflow applies only missing assignments at exact resource scopes. It never grants
Owner. For the supported native model path, common assignments include:

- Cognitive Services OpenAI User for the project managed identity on the native model
  account
- Foundry User for the project managed identity on the Foundry project
- Monitoring Reader on the connected Application Insights component
- Privileged Monitoring Data Reader on the linked Log Analytics workspace when trace
  content is protected
- Foundry User or Foundry Project Manager for the current user on the selected project,
  depending on the sample agent type

If the caller cannot create a required assignment, the workflow stops before mutation
and produces an exact admin handoff with principal, role, scope, and Azure CLI command.
After an administrator applies it, the workflow reruns doctor to verify access before
enabling scheduling.

See the skill's [permission reference](.agents/skills/agent-insights-onboarding/references/permissions.md)
for the complete policy and prerequisites.

## Safety and cost

- Review Azure Monitor, Foundry model, and Hosted Agent pricing before using scratch
  mode.
- The sample sends six healthy and five intentionally faulty requests with concurrency
  capped at two.
- Existing agents are never invoked without a separate opt-in.
- Existing agents, versions, monitors, connections, and role assignments are never
  deleted or overwritten.
- Cleanup operates only on resources whose receipt and ownership tags match.

<details>
<summary>Advanced: standalone CLI and development</summary>

The skill drives the CLI automatically. To inspect its commands:

```shell
python .agents/skills/agent-insights-onboarding/scripts/agent_insights_onboard.py --help
```

The CLI writes sanitized, ignored receipts under `.agent-insights/runs/`. Receipts
contain resource and operation IDs, never tokens, keys, connection strings, headers, or
raw customer telemetry.

For development:

```shell
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
pytest
ruff check .
mypy .agents/skills/agent-insights-onboarding/scripts
```

Live tests require a disposable Agent Insights-enabled subscription and are never run
by pull-request CI.

</details>

## License

[MIT](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for adapted public
sample patterns.
