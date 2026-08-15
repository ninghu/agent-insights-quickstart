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

| Principal | Role ID | Scope | Purpose |
| --- | --- | --- | --- |
| Project managed identity | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` (Cognitive Services OpenAI User) | Parent Foundry account | Exact native-model inference data actions required by scheduled insights |
| Project managed identity | `53ca6127-db72-4b80-b1b0-d745d6d5456d` (Foundry User) | Foundry project | Direct/native deployment metadata read during scheduled admission |
| Project managed identity | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` (Monitoring Reader) | Application Insights component | Component-centric telemetry query |
| Project managed identity | `dbc9c667-e97f-4491-aee6-90b9cf960190` (Privileged Monitoring Data Reader) | Linked Log Analytics workspace | Protected `AppGenAIContent` reads |
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
- A Hosted Agent receives a separate platform-created identity. It has implicit access
  to project model/session capabilities when it uses the project endpoint. The workflow
  does not add speculative account-wide roles to this identity.

Access for one identity never substitutes for another.

Scheduled Agent Insights resolves agent, connection, and most model metadata through
service-to-service calls. For a direct/native project deployment, it still reads the
deployment through the project data plane with Project MI, so Foundry User is required
at the **project** scope. It separately validates the exact
`Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action` on the
model account; Cognitive Services OpenAI User supplies that action without granting
Foundry User on the parent account. API-key and project-Responses model connections have
different authentication paths.

## References

- [Foundry RBAC](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry)
- [Hosted agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Protected Log Analytics tables](https://learn.microsoft.com/azure/azure-monitor/logs/protected-tables-configure)
