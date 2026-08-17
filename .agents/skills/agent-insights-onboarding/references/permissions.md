# Permissions

The workflow checks effective permissions before making writes and applies only missing
built-in roles at exact resource scopes. It never grants Owner.

## Caller prerequisite

The signed-in Azure CLI user needs the effective resource create/delete actions required
by the selected path and both `Microsoft.Authorization/roleAssignments/write` and
`Microsoft.Authorization/roleAssignments/delete` at every planned scope. A common
combination is Contributor plus Role Based Access Control Administrator. Equivalent
custom roles are supported.

If this prerequisite is absent, `doctor` stops before mutation and emits an
administrator handoff. It does not attempt to elevate the caller.

The handoff lists each missing principal, role definition ID, exact scope,
deterministic assignment ID, and `az role assignment create` command. After an
administrator applies it, rerun doctor and require `status: ready`; user confirmation
alone never authorizes scheduling.

## Default remediation policy for native model deployments

Hosted one-off runs use the current caller through OBO. The workflow plans Project MI
assignments only for scheduled generation; selecting one-off does not create or repair
Project MI Foundry-account or monitoring roles.

| Principal | Role ID | Scope | Purpose |
| --- | --- | --- | --- |
| Project managed identity | `53ca6127-db72-4b80-b1b0-d745d6d5456d` (Foundry User) | Parent Foundry account | Native project metadata and model inference required by scheduled insights |
| Project managed identity | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` (Monitoring Reader) | Application Insights component | Scheduled component-centric telemetry query |
| Project managed identity | `dbc9c667-e97f-4491-aee6-90b9cf960190` (Privileged Monitoring Data Reader) | Linked Log Analytics workspace | Scheduled protected `AppGenAIContent` reads |
| Current user | `53ca6127-db72-4b80-b1b0-d745d6d5456d` (Foundry User) | Foundry project | Prompt Agent management and invocation |
| Current user | `eadc314b-1a2d-4efa-be10-5d325db5065e` (Foundry Project Manager) | Foundry project | Source-code Hosted Agent deployment |
| Current user | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` (Monitoring Reader) | Application Insights component | Telemetry and delegated on-demand reads |
| Current user | `dbc9c667-e97f-4491-aee6-90b9cf960190` (Privileged Monitoring Data Reader) | Linked Log Analytics workspace | Protected content reads |

Privileged Monitoring Data Reader is added only when the linked table is protected or
the target project reports that protected access is required. It is not assigned by
default for the current requests/dependencies trace path.

Role display names are documentation only. The implementation uses IDs, deterministic
assignment names, principal types, and exact resource IDs, then validates every create
response.

## Identity boundaries

- The caller operates the public Agent Insights and Foundry APIs.
- The project managed identity runs scheduled Agent Insights operations against the
  customer's telemetry and analysis model.
- Hosted one-off runs use caller OBO; their model and telemetry authorization comes
  from the current user rather than Project MI.
- A Hosted Agent receives a separate platform-created identity. It has implicit access
  to project model/session capabilities when it uses the project endpoint. The workflow
  does not add speculative account-wide roles to this identity.

Access for one identity never substitutes for another.

For a direct/native project deployment, account-scoped Foundry User is inherited by the
child project and covers both project metadata reads and native model inference. The
workflow therefore does not add separate project-scoped Foundry User or Cognitive
Services OpenAI User assignments.

An Entra-authenticated model connection targeting another account is an exception. Its
external model-account scope and inference authorization must be discovered and handled
separately; the workflow never applies the native account policy to a guessed scope.
API-key and project-Responses model connections have different authentication paths.

## References

- [Foundry RBAC](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry)
- [Hosted agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Protected Log Analytics tables](https://learn.microsoft.com/azure/azure-monitor/logs/protected-tables-configure)
