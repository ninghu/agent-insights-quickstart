# Scratch environment

Scratch mode creates one tagged resource group containing:

- Foundry account, project, and low-capacity model deployment
- Log Analytics workspace and workspace-based Application Insights component
- Account and project Application Insights connections
- Exact-scope role assignments
- Either a Prompt Agent or a Python 3.13 source-code Hosted Agent

The user chooses the subscription, supported region, available model, and sample agent
type. Model availability and quota are checked before deployment.

## Sample traffic

The sample is an order-status assistant with a deterministic `lookup_order` dependency:

- six healthy requests;
- five intentionally faulty requests;
- healthy requests remain the majority;
- concurrency is at most two;
- model and tool calls have no retries.

The CLI records response, session, and trace IDs and polls Application Insights until
every expected root is correlated to the exact agent/version. It never sends extra
traffic to compensate for delayed ingestion.

## Completion and cleanup

After ingestion, the workflow creates an enabled monitor with a conservative cadence,
waits for its immediate scheduled run, and requires at least one insight. Resources stay
available for exploration.

The final receipt prints an exact cleanup command. Cleanup verifies the resource group
ID, `created-by`, run ID, and initiating object ID before deletion.
