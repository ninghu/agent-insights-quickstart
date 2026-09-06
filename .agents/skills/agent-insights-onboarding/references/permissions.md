# Permissions

The workflow checks effective permissions before writes and plans only missing roles
at exact resource scopes. It never grants Owner. A prepared bug-bash project should
already have participant access configured by its organizer.

## Prepared-project participant access

Use an interactive Azure CLI **user** in `AzureCloud`. The caller needs effective
access to create, invoke, inspect, and later delete the run's sample/monitor, use the
selected deployment, read the chosen project/connection, and query normal monitoring
data. Doctor verifies the selected path, including required management-plane actions.

| Principal | Role ID | Scope | Purpose |
| --- | --- | --- | --- |
| Current user | `53ca6127-db72-4b80-b1b0-d745d6d5456d` (Foundry User) | Foundry project | Prompt Agent management and invocation |
| Current user | `eadc314b-1a2d-4efa-be10-5d325db5065e` (Foundry Project Manager) | Foundry project | Source-code Hosted Agent deployment |
| Current user | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` (Monitoring Reader) | Application Insights component | Local preflight, ingestion verification, and participant telemetry reads |
| Project managed identity | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` (Monitoring Reader) | Application Insights component | Service-side telemetry reads, including one-off Insights |

Do not request subscription-wide Owner or RBAC administration merely to participate.
If all access and connections are preconfigured, there is no reason to assign new
roles. Equivalent effective/custom access can satisfy prerequisites.

Bug-bash uses caller-delegated access (OBO) for model access, for both samples.
The current hosted service reads telemetry as the Project MI even for an on-demand run.
The project therefore needs its system identity and exact-component Monitoring Reader.
Do not grant Project MI Foundry-account/model-inference roles or enable scheduling for
this one-off path. A successful caller query does not prove the service MI can read data.

## Missing prerequisites and administrator handoff

Only when the plan must create/delete assignments does its executor need
`Microsoft.Authorization/roleAssignments/write` and
`Microsoft.Authorization/roleAssignments/delete` at those exact scopes. Infrastructure
provisioning, a missing connection, and model deployment may require additional actions.
Scratch therefore has more prerequisites than the prepared path.

If a prerequisite is missing, doctor stops before mutation and returns an
`admin_handoff`; it does not elevate the caller. Show the exact principal, role
definition ID, scope, deterministic assignment ID, and returned command to the
organizer/administrator. After they apply it, rerun the same doctor and require
`status: ready`. A verbal confirmation is not proof of access.

A scoped combination such as Contributor and Role Based Access Control Administrator
can be appropriate for an administrator executing a reviewed provisioning plan; it is
not a blanket participant requirement. Do not broaden scopes to work around failures.

Role display names are documentation only. The implementation uses role IDs,
deterministic names, principal types, and exact resource IDs and validates responses.

## Protected data and identity boundaries

Normal requests/dependencies monitoring is the event path. If the linked table is
protected or the project explicitly requires protected access, surface that prerequisite:
Privileged Monitoring Data Reader (`dbc9c667-e97f-4491-aee6-90b9cf960190`) belongs only
on the exact linked Log Analytics workspace and only for the identity that needs it.
Do not grant it by default, change table protection, or turn the event into a separate
protected-content scenario.

- The caller operates Foundry/Agent Insights APIs and supplies one-off model
  authorization. The service's telemetry identity is the Project MI. Access for one
  identity never substitutes for the other's required scope.
- Keep infrastructure identities required by Foundry and Hosted deployment. A Hosted
  Agent has a separate platform-created identity; do not add speculative account-wide
  roles to it.
- An Entra-authenticated model connected from another account is an exception to native
  deployment policy. Discover its actual external account scope and authorization
  separately; do not guess scopes or reuse a native role plan.

## Legacy standard-profile compatibility

The default standalone `standard` profile still supports scheduled Insights. That
non-event path may require Project MI Foundry User on the parent Foundry account,
Monitoring Reader on Application Insights, and protected-reader access on the linked
workspace only when needed. Native account-scoped Foundry User covers child-project
metadata and inference; do not add separate project-scoped Foundry User or Cognitive
Services OpenAI User assignments for that native MI path. These are compatibility
rules, not steps or permission requests for the bug-bash participant.

## References

- [Foundry RBAC](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry)
- [Hosted agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Protected Log Analytics tables](https://learn.microsoft.com/azure/azure-monitor/logs/protected-tables-configure)
