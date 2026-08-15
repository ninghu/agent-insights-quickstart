# Existing resources

Use this path to onboard a customer-owned Foundry project without replacing existing
state.

## Required selections

- Foundry project endpoint
- Existing agent
- Analysis model deployment
- Application Insights component only when the project has no usable connection

Resolve the endpoint to its subscription and ARM project through Azure Resource Graph
across enabled subscriptions in the active tenant. Ask for a subscription only if this
lookup cannot resolve one project. Discover remaining values through Azure and Foundry
APIs. Reject malformed resource IDs, cross-subscription mismatches, non-HTTPS project
endpoints, multiple ambiguous Application Insights connections, and unavailable agents
or models.

Discover model deployments and recommend a current GPT-5+ deployment. If the project
has none, use [model selection](model-selection.md) to find a quota-backed current model
and guide a non-overwriting deployment before doctor.

## Mutation rules

- Check project-level Application Insights connections before any monitor operation.
  Reuse one valid connection. If none exists, discover a component in the project
  resource group or subscription and create the missing account/project connections.
  Multiple connections fail as ambiguous; never guess or delete one.
- Enable a missing project system identity only on the exact selected project.
- Create an App Insights connection only when none exists and the user selected the
  target component.
- Add only missing role assignments from the permission policy.
- If a monitor exists, preserve its model, cadence, overview, and enabled state.
- If no monitor exists, create one only after telemetry and dependency checks pass.
- Do not invoke the selected agent unless the user separately opts in after seeing the
  name and the bounded request count.
- Explicit cleanup preserves the monitor and project identity. It removes only
  connection and role resources whose live principal, role, scope, target, and
  deterministic name still match the frozen plan.

## Completion

Require recent correlated traces, a terminal successful run, and at least one returned
insight. Reuse an existing successful run and insight collection instead of creating a
duplicate run. If no usable result or fresh traces exist and invocation is declined,
stop with the exact trace-generation requirement rather than reporting partial success.

Ask whether to enable scheduled generation after the first result. When selected, enable
the monitor with its existing interval or the new-monitor default of 24 hours. Report the
effective interval and next scheduled run. Choosing one-off leaves a disabled monitor
disabled; an already-enabled monitor is never disabled.
