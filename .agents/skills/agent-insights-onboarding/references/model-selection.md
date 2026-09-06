# Insight generation model

Agent Insights is a reasoning-heavy synthesis workload. The organizer should prepare
a suitable current deployment with quota before the event. Prefer a current GPT-5-class
or newer model returned by discovery; a model family alone is not blanket qualification.
Do not select a deployment by a fixed model ID or release date copied from this guide.

## Prepared project first

Run `discover deployments --subscription-id <id> --project-resource-id <id>` first.
Reuse an available, suitable organizer-selected deployment rather than creating another
one for each participant. Verify its current model/version, supported capabilities,
deployment SKU, capacity, and quota through discovery and doctor.

If there is no suitable deployment, run
`discover models --subscription-id <id> --location <region> --project-resource-id <id>`.
For explicitly chosen scratch mode, discover candidates in the approved subscription
and region before creating the account/project; omit a project ID that does not exist.
Use the actual returned candidate metadata, not the standalone CLI's legacy defaults.

Discovery returns current chat/Responses-capable GPT-5+ candidates with quota and, when
an account is available, a suggested deployment name and exact deployment command.
Do not recommend GPT-4-class or older models for production insight generation.

## Deployment is a separate reviewed choice

Before running the command:

- Show the discovered model, version, SKU, capacity, target account, and cost
  implications. Do not invent a cost estimate the service has not returned.
- Ask for confirmation. Prefer an organizer handoff if the prepared project is missing
  this prerequisite.
- Refuse to overwrite a deployment with different model metadata.
- If deployment permission is missing, give the exact returned command to an
  administrator, not an ad-hoc account mutation.
- Verify the resulting deployment and rerun doctor before onboarding.

The existing CLI capacity default is `30`; verify it against current model/SKU quota
and concurrent participants rather than treating it as universal sizing. The bounded
Prompt sample can make up to 22 model calls because tool calls have a continuation
turn. Inadequate capacity can cause throttling and incomplete execution, not an
insight-quality verdict.

Keep the analysis deployment and the Copilot CLI session's own model distinct in review
and feedback. Record either only when reliably known. Never invent the Copilot model
from the chosen Foundry deployment or create a separate hosted judge.
