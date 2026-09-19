# Agent Insights Quickstart

Try **Microsoft Foundry Agent Insights** with **Copilot CLI**. Run a sample Agent,
then validate the generated insights yourself in the Foundry portal.

## Quick start

**1. Clone the repo and open Copilot CLI:**

```shell
git clone https://github.com/ninghu/agent-insights-quickstart
cd agent-insights-quickstart
copilot
```

**2. Launch the skill by pasting this prompt into Copilot CLI:**

```text
Create a test agent for me to try out Agent Insights using the agent-insights-onboarding skill.
```

The skill is bundled in `.agents/skills/agent-insights-onboarding`. No separate
installation is needed. Copilot will ask for your **Foundry project endpoint** and
whether to create a **Prompt** or **Hosted** sample Agent, then guide the rest.

## What you need

- Git, **Copilot CLI**, Python **3.13+**, and Azure CLI **2.80+**.
  Sign in with `copilot login` and `az login` if needed.
- An approved test project in an **Agent Insights-enabled Azure public-cloud
  subscription**, with a model deployment, Application Insights, and required access.
  Get the endpoint from your organizer; you do not need an existing Agent.

## What to expect

Copilot checks access, creates your chosen sample, sends **6 healthy + 5 faulty
requests**, and runs Insights **once, without scheduling**. The first Insights run may
take **10-20 minutes**, plus deployment and ingestion.

Once Insights generation finishes, Copilot provides a **Foundry link** for you to open
and validate the results yourself in the portal. Copilot does not automatically open
the portal, assess the results, or ask for a rating or comment.

**AI preliminary assessment and feedback recording are opt-in.** Ask Copilot if you
want either. **Feedback is saved locally, not automatically submitted.** See the
[review and feedback guide](.agents/skills/agent-insights-onboarding/references/quality-review.md).
No suggested fix is applied automatically.

After review, use the cleanup command Copilot prints **for your own sample run**.
Do not delete the shared project or resource group. Azure charges may continue while
resources remain. Do not share credentials or raw telemetry.

## Need help?

If the skill is missing, run `copilot skill list` from this repo and restart Copilot CLI.
For a stalled run, keep its run directory and ask Copilot to resume without replaying traffic.

[Troubleshooting](.agents/skills/agent-insights-onboarding/references/troubleshooting.md)
| [Permissions](.agents/skills/agent-insights-onboarding/references/permissions.md)
| [Organizer checklist](.agents/skills/agent-insights-onboarding/references/organizer-guide.md)
| [Contributing](CONTRIBUTING.md)

Community preview, not an official support channel. [MIT License](LICENSE).
