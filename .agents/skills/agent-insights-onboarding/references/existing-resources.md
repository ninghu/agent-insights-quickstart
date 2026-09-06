# Prepared existing project

This is the **recommended bug-bash path**. Reuse the organizer's Foundry project,
deployment, and telemetry connection, but create a fresh owned sample Agent. The
participant does not select or evaluate an existing customer Agent.

## Discovery and required choices

1. Establish existing versus scratch before requesting Azure values. Ask for the
   prepared project's endpoint first.
2. Run `discover project --project-endpoint <endpoint>`. Resolve its subscription and
   ARM project across enabled subscriptions in the active tenant. Ask for a
   subscription only if discovery cannot resolve a unique project.
3. Choose the fixed **Prompt** or **Hosted** sample. Pass `--create-sample-agent`;
   never pass `--agent-name` in this path.
4. Run `discover connections` with the resolved `--subscription-id` and
   `--project-resource-id`. Reuse exactly one usable Application Insights connection.
   A missing connection is an organizer prerequisite; if remediation is requested,
   discover components in the project's resource group first, then subscription, and
   require an explicit component selection for the reviewed plan. Multiple matching
   connections are an ambiguity to stop on, not permission to delete one.
5. Run `discover deployments` with the same context and select a suitable current
   deployment. Use [model selection](model-selection.md) if none is suitable. Do not
   replace a deployment or guess a fixed model ID.

Reject malformed resource IDs, cross-subscription mismatches, non-HTTPS project
endpoints, ambiguous connections, and unavailable models. The target subscription must
be Agent Insights-enabled and the signed-in identity must be an interactive Azure user.

## CLI contract

Copilot drives these commands; this is a reference, not a replacement for the
conversation. Use the skill virtual environment's Python. For example, in PowerShell
with that environment active:

```powershell
$cli = ".agents\skills\agent-insights-onboarding\scripts\agent_insights_onboard.py"
$configuration = @(
  "--profile", "bug-bash",
  "--mode", "existing",
  "--subscription-id", "<discovered-subscription-id>",
  "--project-resource-id", "<discovered-project-resource-id>",
  "--project-endpoint", "<organizer-project-endpoint>",
  "--agent-type", "prompt",
  "--create-sample-agent",
  "--model-deployment-name", "<selected-deployment>"
)
python $cli doctor @configuration
```

Use `hosted` instead of `prompt` for the Hosted sample. Only add
`--application-insights-resource-id` when a missing connection requires an explicitly
selected component. No scheduled flag belongs in this configuration.

Require doctor `status: ready` before mutation. A read-only Azure plan preview is
available with `python $cli plan @configuration`. To proceed after readiness:

```powershell
python $cli onboard @configuration
```

Onboard freezes and prints the plan, run directory, and progress, then applies the
reviewed changes. A new run has its own deterministic Agent name and ownership
metadata. Do not reuse a previous run ID for a different participant/sample.

## Shared-resource boundaries

- Reuse organizer-preconfigured roles and connections. Plan only missing exact-scope
  assignments. No subscription-wide Owner or scheduling-only Project MI access is
  required for the one-off flow; see [permissions](permissions.md).
  The separate service telemetry path still requires Project MI Monitoring Reader on
  the connected component, even when the monitor remains disabled.
- Preserve existing project infrastructure and other runs' Agents, versions, monitors,
  connections, model deployments, and assignments.
- Send only the fixed six healthy/five faulty requests to the newly owned sample.
- After verifying ingestion, submit one manual Insights run for this sample. Do not
  enable/disable a pre-existing monitor or substitute unrelated historical results.
- Resume only the same recorded run. Never replay traffic or submit a second run to
  improve quality.

## Review and cleanup

A successful service run with valid review provenance initially produces a
`review_pending` receipt with structural counts and review paths. Empty insights or
missing concrete fixes remain explicit quality findings for
[AI and overall human review](quality-review.md); they are not evidence of quality
success. Technical failures still fail.

The CLI later synchronizes the existing receipt to `complete` only when current AI
and rated overall human records are present. This does not approve insight quality or
prove a fix; unable/deferred feedback remains a valid explicit response.

Keep the printed portal link, run directory, and local evidence. Do not clean up merely
because the service completed. After review or an explicit decision to end the run,
use its printed cleanup command. Cleanup verifies the frozen plan, deterministic
names, exact Agent/version and live ownership, including partial ownership journals.
It removes only recorded owned resources and preserves the prepared project and
other participants' resources. Never replace this with a prefix sweep.

The standalone default `standard` profile retains legacy selected-Agent capabilities;
they are not this skill's participant path or quality acceptance coverage.
