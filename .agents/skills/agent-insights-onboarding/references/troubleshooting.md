# Troubleshooting

Keep execution failures, quality weaknesses, and evidence uncertainty separate. A
completed service job or structural fix count is not semantic quality approval.

| Failure | Meaning | Action |
| --- | --- | --- |
| `unsupported_cloud` | Azure CLI is not using `AzureCloud`. | Select the approved Azure public-cloud context. Sovereign clouds are not supported. |
| `feature_unavailable` | The selected subscription/project is not Agent Insights-enabled. | Ask the organizer for an explicitly enabled target. Do not silently switch subscriptions or provision scratch. |
| `insufficient_preflight_permission` | A required resource or role action is missing. | Show the exact `admin_handoff`; after an administrator completes it, rerun doctor and require `ready`. Do not grant Owner. |
| `ambiguous_app_insights_connection` | More than one project connection can satisfy the request. | Hand the ambiguity to the organizer. Never guess or delete a connection. |
| `model_unavailable` | The model/version/SKU is unavailable or quota is insufficient. | Use deployment/model discovery and rerun doctor. Do not replace another deployment. |
| `role_propagation_timeout` | A recorded assignment has not become effective. | Keep the run directory and partial journal; use the returned stage-specific recovery instructions. Do not blindly recreate an assignment. |
| `ingestion_timeout` | Not all expected trace IDs are correlated yet. | Use `status --run-dir <path>` for this run; never replay traffic to fill the gap. |
| `insights_run_failed` | Agent Insights reached a terminal failed state. | Preserve the sanitized stage/error and run references. This remains a technical failure. |
| `insights_admission_unconfirmed` | A manual request lost its acknowledgement and cannot yet be uniquely reconciled. | Use `status` to inspect the same monitor again. Do not submit a replacement run or replay traffic. |
| `quality_run_scope_ambiguous` | The monitor has additional runs or its single manual run cannot be confirmed. | Preserve the run references. Do not attribute monitor-wide results to the original run or generate a replacement. Already frozen review evidence remains local and unchanged. |
| `provisioning_outcome_unconfirmed` | A connection or role write has no confirmed outcome. | Preserve the pending mutation journal and use ownership-checked cleanup or administrator reconciliation. Do not replay the write. |
| `cleanup_unconfirmed_mutations` | A pending resource exists but its ownership cannot be established safely. | Cleanup is incomplete, not successful. Known owned resources may already be removed; retain the unknown resources for administrator reconciliation. A later confirmed absence can complete cleanup. |
| `ownership_mismatch` | A resource or receipt does not prove this run's ownership. | Stop. Inspect read-only evidence or involve the organizer; never claim, reuse, or delete the mismatched resource. |
| `agent_creation_not_owned` | The selected run ID already names a remote sample without a prior local creation receipt. | Do not adopt or invoke it. Keep the other run's resources intact and use a new run ID for a fresh sample. |
| `quality_baseline_not_frozen` | The plan did not capture baseline provenance before execution. | Preserve the run and report the provenance failure. Never backfill a historical plan from current source or replay traffic. |
| `quality_baseline_mismatch` | Quality-review validation found stored/deployed content or a descriptor that differs from the frozen baseline, including deliberately updated source/catalog/fixture metadata. | Preserve the original artifacts and resolve the mismatch with the maintainer. Do not rewrite receipts or relabel current source as the original baseline. |
| `quality_review_not_ready` | Frozen review input or complete ingested/manual-run evidence is not available yet. | Follow this run's `status --run-dir` recovery guidance. Do not invent input, replay traffic, or fill human feedback to bypass readiness. |
| `quality_baseline_changed` | Catalog loading found an asset/hash disagreement, or onboarding's pre-write check found a descriptor changed since planning. | Restore a matching reviewed catalog/assets pair; do not merely rehash an unexpected edit. Existing runs require their original baseline, with no historical backfill or replacement traffic. |

## Quality and evidence findings

A service-side `Forbidden` can involve an identity different from the CLI caller.
Verify caller model access separately from Project MI Monitoring Reader on the
connected Application Insights component. The current service uses the latter for
one-off telemetry reads as well. Do not enable scheduling or grant MI inference roles
to mask a telemetry permission gap. Preserve failed attempts rather than rewriting them.

The public API represents a manual trigger as `on_demand`; the CLI normalizes this to
`manual` in its execution/quality contract. Unknown trigger values are not guessed.

- **No insights, prose-only fixes, or malformed changes:** bug-bash retains the
  successful service result and explicit structural findings for review. Do not call
  quality passed or create a replacement run. The standard profile's legacy
  `empty_insights`/concrete-fix failure contract is not the bug-bash review contract.
- **Missing healthy/fault evidence or tool execution:** classify the fixture/execution
  problem or evidence uncertainty before blaming Insights. Correlated trace IDs alone
  do not prove the known defect was exercised.
- **Weak/unsupported diagnosis or fix:** preserve the result, explain the supporting
  evidence and limits in the AI preliminary assessment, and collect the overall human
  response. A low rating is valid feedback.
- **Partial insight collection or uncertain provenance:** disclose coverage limits.
  Never assume the first page is complete or borrow unrelated historical insights.
- **Stale review input:** reread local `review prepare`/`review status` output and use
  its digest. Do not relabel an old AI/human record with a new digest. Reassess the actual
  evidence and obtain new human feedback if the reviewed result changed.
- **No human answer:** leave feedback pending. Explicit unable-to-judge/deferred or
  no-comment responses are allowed; silence is not a completed review.

See [quality review](quality-review.md) for the rubric and exact local commands.

## Recovery without replay

`Permission denied and could not request permission from user` is a Copilot CLI
tool-approval failure, not proof of an Azure RBAC failure. In a headless session,
`--allow-all-tools` does not by itself grant path/URL access. ARM resource-ID arguments
may be interpreted as paths. Use an appropriately authorized session or the interactive
approval flow; never weaken organization rules, change explicit deny policies, or
obfuscate an operation to avoid a denial.

Receipts are under `.agent-insights/runs/<run-id>/`. Show this directory and the latest
stage early. Keep the frozen plan and partial resource journal even if no final receipt
exists.

These runtime directories are git-ignored. Inspect explicit paths or use the reviewed
`status`/`review status` commands; an empty glob/search result is not evidence that a run
or its traffic receipt does not exist.

Before traffic, follow the returned recovery information using the original run ID
and arguments. Resolve uncertain Azure outcomes read-only; do not replay a create
request or invent a new ownership claim. After traffic has started, `status` continues
or reconciles the same recorded run. Interrupted/incomplete traffic remains explicit,
not resumable success.

`review prepare` and `review status` inspect already persisted local evidence. They do
not recover Azure ingestion, admit another run, or change the fixture. If preparation
is unavailable because execution never reached a reviewable result, preserve the
technical failure rather than manufacturing a review bundle.

If evidence was frozen but execution stopped before the final handoff, `status`
finalizes from that preserved snapshot. It does not replace the supporting receipts
with a newer cost estimate or another monitor-wide insight collection.

## Cleanup and sharing

Keep resources while a participant reviews the portal. After review or explicit
authorization to end an incomplete run, use only the printed ownership-checked
cleanup command. Partial journals may identify resources even before a full deployment
receipt exists. Confirmed already-removed resources are different from ownership
mismatches or unknown service errors; never conceal the latter with a broad sweep.

Sanitized does not mean automatically publishable. Inspect receipts for
environment-specific identifiers before sharing. Never share tokens, keys, headers,
connection strings, raw customer telemetry, or complete unfiltered SDK responses.
Use the [feedback checklist](quality-review.md#feedback-material), not an automatic upload.
