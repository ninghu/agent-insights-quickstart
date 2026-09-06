# Organizer readiness checklist

Prepare the shared infrastructure before the event so participants can focus on
insight quality, not subscription administration. The participant experience is
**Copilot CLI**, a newly owned fixed sample, and one manual Insights run.

## Select the environment explicitly

- [ ] Select an approved disposable/test Foundry project in an **Agent Insights-enabled
  Azure public-cloud subscription**. Do not infer the target from a runner's current
  login, a copied example ID, or this repository.
- [ ] Provide the Foundry project endpoint. Verify `discover project` resolves the
  intended subscription and ARM project without ambiguity.
- [ ] Keep customer Agents and raw customer telemetry out of the event. A shared test
  project is acceptable; each participant must create a distinct receipt-owned Agent.
- [ ] Identify the person responsible for access handoffs, costs, and cleanup. Scratch
  is a separately selected fallback, not automatic remediation for an unready project.

## Send a self-service invitation

Share the merged repository revision or an agreed release, not an unmerged development
branch. Provide the environment details separately; do not hardcode an organizer's
temporary resource IDs or endpoint into the public README.

Copy this template and fill in every applicable field before inviting participants:

```text
Repository / revision: <merged repository link and commit or release>
Approved test project endpoint: <endpoint shared through the approved channel>
Preferred model deployment: <existing deployment name>
Sample assignment: <Prompt, Hosted, or both as separate fresh sample runs>
Feedback destination: <accessible bug form plus channel for ratings/no-bug feedback>
Access / troubleshooting contact: <organizer or administrator>
Review and cleanup instructions: <retention window and owner of shared infrastructure>

Start with the repository README in an interactive Copilot CLI session.
Recording a score locally does not submit feedback; use the destination above.
Do not delete the shared project, model, monitoring resources, or resource group.
```

- [ ] Confirm everyone can access the repository and the selected feedback destination.
  The service bug form requires access to its internal project; provide an approved
  alternative contact when that access is unavailable.
- [ ] Explain what to send when no bug is found: the participant's overall rating and
  comment, with a sanitized sample/revision reference. Do not use the bug tracker as an
  invented automatic collection mechanism.
- [ ] Check the README flow with ordinary participant access, not only an organizer's
  Owner/admin account. Resolve model-discovery, project, telemetry, and permission
  handoff prerequisites before the event.

## Model, quota, and telemetry

If the organizer explicitly chooses to create an isolated test environment, use the
reviewed CLI's `prepare-project --profile bug-bash --mode scratch` with the approved
subscription, region, discovered model parameters, and `--agent-type hosted` when both
sample types will be exercised. Run `doctor` first with the same configuration.
Preparation creates only the owned resource group, Foundry project/model, monitoring,
connections, and caller roles. Its `project_ready` receipt contains the endpoint and
scoped cleanup command; it generates no Agent, traffic, or Insights run.

Use that endpoint in separate `--mode existing --create-sample-agent` participant runs.
Do not reuse the infrastructure run ID for onboarding, and do not clean up its resource
group while participant review is still in progress.

For an already prepared, receipt-owned environment, `prepare-project --refresh-access`
with the same original arguments and run ID verifies its group ownership and applies
only missing exact-scope roles. Each refresh has a separate plan and receipt under the
parent run; it does not recreate the project/model or generate traffic.

- [ ] Use `discover deployments` to select a suitable **current** model deployment.
  If necessary, use `discover models` for the actual region and review the returned
  model/version/SKU/capacity before a non-overwriting deployment.
- [ ] Verify sufficient quota for concurrent participants, the sample's bounded traffic,
  and insight generation. Do not hardcode model IDs from an old guide or assume a
  family name guarantees quality.
- [ ] Configure **one usable Application Insights connection** for the project and
  verify its linked component/workspace. Multiple matching connections must be resolved
  by the organizer, not guessed at or deleted by the skill.
- [ ] Verify ordinary requests/dependencies ingestion and monitoring access. Protected
  trace content is not a dedicated event scenario; if existing protection blocks access,
  handle the exact prerequisite without changing protection or sharing raw content.
- [ ] Preserve the selected deployment and connection across participants. No
  scheduling-only Project MI grant is needed for this one-off event.
- [ ] Grant the Project MI Monitoring Reader on the connected Application Insights
  component: the current service uses this identity for telemetry even on one-off runs.
  Keep this shared prerequisite in the organizer environment; no MI model-inference
  role is needed for caller-delegated one-off model access.

## Participant access and workstation

- [ ] Preconfigure the caller's effective project access: Foundry User for the Prompt
  sample or Foundry Project Manager for source-code Hosted deployment, plus Monitoring
  Reader on the selected Application Insights component and any specific actions
  identified by doctor. See [permissions](permissions.md).
- [ ] Let doctor identify missing exact-scope actions. Participants with ready
  infrastructure do **not** need subscription-wide Owner or blanket RBAC administration.
  Have an administrator handle any returned principal/role/scope handoff and recheck it.
- [ ] Ensure participants have Copilot CLI, Python **3.13+**, Azure CLI **2.80+**, and Git.
  Obtain approval before installing a missing tool via its vendor installer.
- [ ] Use an ignored virtual environment and the skill's pinned requirements. Do not
  install dependencies into the system interpreter.
- [ ] Require interactive Azure CLI **user** login and verify the selected context.
  Service-principal/OIDC automation is not actual user-delegated skill acceptance.
- [ ] Confirm a fresh Copilot CLI conversation discovers and loads the project skill;
  passing the packaging check alone does not establish this.

## Run and review boundaries

- [ ] Offer the fixed Prompt false-success or Hosted 20 ms/80 ms timeout sample. Keep
  **six healthy plus five faulty requests** per selected sample; no new/random defects.
- [ ] Confirm the configuration uses `--profile bug-bash`; existing mode uses
  `--create-sample-agent` and no `--agent-name`. There is no scheduling question.
- [ ] Surface the run directory, frozen plan, and stage progress early. Show the portal
  link immediately when the manual Insights run is admitted; allow roughly 10–20
  minutes for that first run plus provisioning and ingestion.
- [ ] Keep execution status, structural findings, Copilot's AI preliminary assessment,
  and the participant's one overall rating/comment separate.
- [ ] Accept poor/mixed results, unable-to-judge, deferred, and no-comment responses.
  Missing human input remains pending. Never fill acceptance feedback with synthetic
  examples or the AI verdict.
- [ ] Do not apply a suggested fix, rerun traffic, or create another Insights run to
  improve a score. See the [rubric](quality-review.md).

## Retention, cost, and feedback

- [ ] Explain model, monitoring, and Hosted Agent charges and the agreed review window.
  Cost estimates should be reported only when actually available.
- [ ] Preserve participant resources until review or an explicit decision to end that
  run. The separate technical matrix provisions and deletes its own disposable fixture;
  it is not a participant workflow step and cannot replace retained-resource review.
  An automated smoke cleanup must not sweep active participant resources.
- [ ] Use each run's printed, ownership-checked cleanup command. Preserve shared
  project infrastructure, model deployments, connections, pre-existing assignments,
  and other participants' Agents. Keep partial journals for interrupted runs.
- [ ] Verify cleanup outcomes rather than assuming an `always()` job removed
  everything. Use execution-specific manifests, never broad name-prefix deletion.
- [ ] Review sanitized evidence before sharing; no tokens, keys, connection strings,
  raw telemetry, or unfiltered SDK responses. Feedback submission is explicit, not
  automatic. Local `human-review.json` and a completed recording status are not proof
  that the organizer received feedback.

For release acceptance, run separate fresh Copilot CLI Prompt and Hosted conversations
and record the actual human step or its pending/blocking state. A technical matrix
result is not human quality approval. Reconcile documentation with observed behavior
after acceptance.
