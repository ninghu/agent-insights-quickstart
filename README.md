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
| Existing Foundry project | Resolves the endpoint, lets you select an existing Agent or create a new sample Prompt/Hosted Agent in that project, validates the Application Insights connection and first result, and asks whether to enable scheduled generation. |
| Create a new Foundry project | Creates a tagged scratch project and monitoring stack, deploys either a Prompt Agent or source-code Hosted Agent, sends bounded sample traffic, enables Agent Insights, and verifies a first insight. |

For best insight quality, the workflow recommends a current **GPT-5+** model. If the
project has no suitable deployment, it finds quota-backed candidates and helps deploy
one after confirmation.

The scratch environment stays available after success. The final receipt identifies
the number of insights returned, includes a direct Foundry agent-monitor link for
reviewing details, identifies cost-bearing resources, and prints an ownership-checked
cleanup command.

The code-based Hosted sample includes a bounded lookup-timeout misconfiguration. Its
first successful onboarding must return at least one independently reviewed concrete
code diff; prose-only output is treated as a demo regression rather than success.

The Prompt sample includes an instruction-owned false-success defect after a failed order
lookup. Its first successful onboarding must return at least one independently reviewed
Prompt change against the system instructions; prose-only output is also a demo regression.

As soon as the first Agent Insights run starts, the CLI prints the same direct portal
link and explains that the first run may take **10–20 minutes**. Open the link immediately
to watch progress; the onboarding agent continues monitoring the job and reports the
verified result when it finishes.

When scheduled generation is selected, enabling the monitor creates its first scheduled
occurrence immediately. The workflow verifies that scheduled run directly and does not
create a duplicate manual run. Manual runs are reserved for one-off onboarding.

## What the workflow changes

The workflow applies only missing assignments at exact resource scopes. It never grants
Owner. One-off runs use the current user's delegated access. When scheduled generation
is enabled on the supported native model path, Project MI assignments include:

- Cognitive Services OpenAI User for the project managed identity on the native model
  account
- Foundry User for the project managed identity on the Foundry project
- Monitoring Reader on the connected Application Insights component
- Privileged Monitoring Data Reader on the linked Log Analytics workspace when trace
  content is protected
The current user receives Foundry User or Foundry Project Manager on the selected
project, depending on the sample agent type, plus monitoring access needed by the
one-off run. Selecting one-off does not grant Project MI model-inference access.

If the caller cannot create a required assignment, the workflow stops before mutation
and produces an exact admin handoff with principal, role, scope, and Azure CLI command.
After an administrator applies it, the workflow reruns doctor to verify access before
enabling scheduling.

See the skill's [permission reference](.agents/skills/agent-insights-onboarding/references/permissions.md)
for the complete policy and prerequisites.

## Safety and cost

- Review Azure Monitor, Foundry model, and Hosted Agent pricing before using scratch
  mode.
- The sample sends six healthy and five intentionally faulty requests. Prompt traffic is
  sequential to preserve one trace per conversation; Hosted traffic is capped at two.
- Newly created sample Agents use the known bounded traffic fixtures. Existing customer
  Agents are never invoked by the quickstart; doctor checks for at least three recent
  correlated traces and asks the user to run normal application traffic when none exist.
- Existing agents, versions, monitors, connections, and role assignments are never
  deleted or overwritten.
- A sample Agent created in an existing project uses deterministic ownership metadata.
  Cleanup removes only its receipt-owned monitor and Agent after live ownership checks.
- Cleanup operates only on resources whose receipt and ownership metadata match.

## Feedback

At the end of onboarding, the agent provides this link. If you find a bug or have an
improvement suggestion, create a bug with the
[Agent Insights bug template](https://msdata.visualstudio.com/Vienna/_workitems/create/Bug?templateId=6d5d4dfe-fd55-45f3-b9c9-f7cc2b0e1835&ownerId=5d069bfc-f7ae-4d93-bee7-c94d439a26a7).

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

The full offline decision matrix runs in normal CI:

```shell
pytest tests/test_path_matrix.py
```

To list or execute the disposable Azure matrix:

```shell
python .agents/skills/agent-insights-onboarding/scripts/agent_insights_live_matrix.py \
  --list-cases

python .agents/skills/agent-insights-onboarding/scripts/agent_insights_live_matrix.py \
  --confirm-live \
  --subscription-id <subscription-id> \
  --location <region>
```

The live matrix requires an interactive Azure CLI user and an Agent Insights-enabled
disposable subscription. It runs every supported Prompt/Hosted, scratch/existing,
created/selected Agent, one-off/scheduled, connection, and protected-content path. Each
case verifies its trigger, insights, concrete-fix contract, direct portal link, receipts,
and cleanup. It may take several hours and incur model, monitoring, and Hosted Agent
charges.

`.github/workflows/live-matrix.yml` provides a guarded weekly/on-demand run on a
self-hosted runner labeled `agent-insights-live`. Set the repository variable
`AGENT_INSIGHTS_LIVE_ENABLED=true` to opt into scheduled runs. Configure the subscription,
region, and optional model variables in the `live-azure` environment and keep an
interactive `az login` session active; service-principal/OIDC authentication is
intentionally rejected because it would not test the customer user-delegated path. An
independent `always()` cleanup job removes only matrix-prefixed resource groups whose
ownership tags match the runner's signed-in user.

Live tests require a disposable Agent Insights-enabled subscription and are never run
by pull-request CI.

</details>

## License

[MIT](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for adapted public
sample patterns.
