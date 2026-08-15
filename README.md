# Agent Insights Quickstart

Set up and try Microsoft Foundry Agent Insights with a guided Agent Skill. Use an
existing Foundry project or create a self-contained scratch environment, then validate
the complete path from permissions and telemetry to a visible first insight.

> [!IMPORTANT]
> This is a community preview sample, not an official Microsoft support channel.
> Agent Insights must already be enabled for the target subscription. The initial
> release supports Azure public cloud only.

## Quick start

Prerequisites:

- Git
- Python 3.13 or newer
- Azure CLI 2.80 or newer
- Copilot or another [Agent Skills](https://agentskills.io)-compatible agent
- An Agent Insights-enabled Azure subscription
- Permission to manage the selected resources and create scoped role assignments

```shell
git clone https://github.com/ninghu/agent-insights-quickstart
cd agent-insights-quickstart
az login
```

The skill creates an ignored virtual environment and installs its reviewed, pinned
Python requirements. It never installs into the system interpreter.

Then ask your agent:

```text
Set up Agent Insights for me.
```

Copilot discovers the skill from
`.agents/skills/agent-insights-onboarding/SKILL.md`. You can also install it with
GitHub CLI:

```shell
gh skill install ninghu/agent-insights-quickstart agent-insights-onboarding
```

## Onboarding paths

| Path | Result |
| --- | --- |
| Existing Foundry project | Discovers the project, agent, model, and Application Insights connection; repairs missing scoped access; preserves existing monitor settings; validates an Agent Insights result. |
| New scratch environment | Creates a tagged Foundry project and monitoring stack, deploys either a Prompt Agent or source-code Hosted Agent, sends bounded sample traffic, enables Agent Insights, and verifies a first insight. |

The scratch environment stays available after success. The final receipt identifies
cost-bearing resources and prints an ownership-checked cleanup command.

## What the workflow changes

The workflow applies only missing assignments at exact resource scopes. It never grants
Owner. Common assignments include:

- Foundry User for the project managed identity on its parent Foundry account
- Monitoring Reader on the connected Application Insights component
- Privileged Monitoring Data Reader on the linked Log Analytics workspace when trace
  content is protected
- Foundry User or Foundry Project Manager for the current user on the selected project,
  depending on the sample agent type

See the skill's [permission reference](.agents/skills/agent-insights-onboarding/references/permissions.md)
for the complete policy and prerequisites.

## Standalone CLI

The skill drives the CLI automatically, but every phase is available directly:

```shell
python .agents/skills/agent-insights-onboarding/scripts/agent_insights_onboard.py --help
```

The CLI writes sanitized, ignored receipts under `.agent-insights/runs/`. Receipts
contain resource and operation IDs, never tokens, keys, connection strings, headers, or
raw customer telemetry.

## Safety and cost

- Review Azure Monitor, Foundry model, and Hosted Agent pricing before using scratch
  mode.
- The sample sends six healthy and five intentionally faulty requests with concurrency
  capped at two.
- Existing agents are never invoked without a separate opt-in.
- Existing agents, versions, monitors, connections, and role assignments are never
  deleted or overwritten.
- Cleanup operates only on resources whose receipt and ownership tags match.

## Development

```shell
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
pytest
ruff check .
mypy .agents/skills/agent-insights-onboarding/scripts
```

Live tests require a disposable Agent Insights-enabled subscription and are never run
by pull-request CI.

## License

[MIT](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for adapted public
sample patterns.
