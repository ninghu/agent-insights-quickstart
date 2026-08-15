# Existing resources

Use this path to onboard a customer-owned Foundry project without replacing existing
state.

## Required selections

- Azure subscription
- Foundry project
- Existing agent
- Analysis model deployment
- Application Insights component only when the project has no usable connection

Discover each value through Azure and Foundry APIs. Reject malformed resource IDs,
cross-subscription mismatches, non-HTTPS project endpoints, multiple ambiguous
Application Insights connections, and unavailable agents or models.

## Mutation rules

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
