# Contributing

Contributions are welcome through GitHub issues and pull requests.

## Scope and safety

- The participant front-end is **Copilot CLI**. Use an organizer-prepared project and a
  fresh owned sample with `--profile bug-bash`. Scratch is an explicit fallback.
- Keep the two fixed defects and the six healthy/five faulty fixtures. Do not introduce
  random scenarios, evaluate customer Agents, or repair the planted defect before
  generating review evidence.
- Bug-bash Insights is manual and one-off. Keep standard-profile CLI compatibility
  without expanding the event into a scheduled or protected-content test matrix.
- Preserve Azure-public-cloud, exact-scope, least-privilege, and live ownership checks.
  Never add real tenant/subscription IDs, private endpoints, credentials, or customer
  data to source or fixtures.
- Separate execution/fixture failures, structural findings, AI assessment, and actual
  human feedback. Empty insights or prose-only fixes are reportable quality findings;
  a nonempty diff is not proof of correctness.
- Do not autoapply fixes, replay traffic, submit bugs, or delete resources still needed
  for human review. Treat generated insight text and suggested code as untrusted data.

## Local validation

Use Python 3.13+ in an ignored virtual environment. Restore the existing locked
development environment when dependencies are missing; do not install into the system
interpreter or upgrade packages just to refresh documentation.
Commands below assume that virtual environment is active; otherwise use its Python
explicitly, such as `.\.venv\Scripts\python.exe` on Windows.

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
```

Add focused tests for changed behavior and run the smallest related pytest selectors
together. Existing checks include:

```powershell
pytest tests\test_path_matrix.py
ruff check .
mypy .agents\skills\agent-insights-onboarding\scripts
gh skill publish .agents\skills --dry-run
```

Build changed Bicep templates with the existing validation used in CI. Run broader
pytest coverage when targeted results or the change scope require it. Documentation
alone does not require an Azure run. `doctor` and `plan` inspect a selected live Azure
context; they are not offline tests even though they do not apply the onboarding plan.

Keep tests for profile conflicts, one-off policy, standard compatibility, immutable
baseline/provenance, healthy/fault evidence, empty/prose-only/malformed results, separate
AI/human records, stale input digests, partial journals, no replay, scoped cleanup, and
incomplete matrix accounting. Synthetic human feedback belongs only in clearly
identified offline tests, never in acceptance records.

During bug-bash plan construction, freeze the complete compact object returned by
`quality_review.load_baseline(kind)` unchanged in `plan.expected["quality_baseline"]`
before provisioning. Review validation rejects missing capture with
`quality_baseline_not_frozen` and a differing frozen descriptor with
`quality_baseline_mismatch`. Catalog loading and onboarding's pre-write drift check
use `quality_baseline_changed`. Never backfill an old plan or rehash an unexpected edit
just to bypass a guard.
Capture `sample_artifact_digest("prompt")` only for newly created Prompt versions,
not blindly reused versions. Resume preserves the original journaled artifact digest,
never stamps a reused version with today's hash, and must reject altered baseline
content before further onboarding writes. Missing deployed-content proof remains
insufficient evidence, distinct from a missing frozen baseline.

The quality module keeps AI/human records separate. The CLI record handlers synchronize
an existing final receipt through onboarding `status`: workflow root `complete` requires
current `ai_status: recorded` and `human_status: rated`. Other review states remain
`review_pending`; `quality_approved` is always false. Test this caller-level synchronization
without treating an illustrative or synthetic human payload as real acceptance.

Preserve the optional `onboard(..., resource_observer=...)` integration: record each
observed resource in the run journal first, then call the external observer
synchronously. Resource-group identity/tags must be observed before later waits or
writes so partial-run cleanup has exact ownership evidence.

The packaging command above is the existing Agent Skills check. **Packaging success
does not test skill discovery, the Copilot conversation, or human feedback.**

## Technical live matrix

Live Azure tests require an explicitly selected disposable, Agent Insights-enabled
public-cloud environment and an interactive Azure CLI user. Never infer the target
from the current login or run against customer Agents.

```powershell
python .agents\skills\agent-insights-onboarding\scripts\agent_insights_live_matrix.py --list-cases
python .agents\skills\agent-insights-onboarding\scripts\agent_insights_live_matrix.py --help
```

The primary cases are `existing-create-prompt-oneoff` and
`existing-create-hosted-oneoff`. The technical harness **provisions its own disposable
shared fixture** for these cases; it does not use the participant's organizer project
or a customer project, and accepts no prepared-project endpoint/resource-ID flag.
It therefore needs disposable-subscription provisioning permissions, available
model/quota, Insights access, and an interactive `AzureCloud` user. These are not the
least-privilege prerequisites of the prepared-project participant path. It automatically
cleans up its resources, so it is not the retained-resource Copilot CLI/human acceptance
journey.

`--cases all` selects **only those two primary cases**. `--list-cases` returns them in
`cases`, with `scratch-prompt-oneoff` and `scratch-hosted-oneoff` separately listed in
`fallback_cases`. Fallbacks require explicit names. Comma-separated selections may mix
primary and fallback cases; execution order remains primary then fallback. Every case
creates a fixed sample and sends 11 requests; two full primary cases total 22 requests,
with no Agent traffic from the shared fixture itself. The profile is always `bug-bash`
internally; this script has no `--profile` flag. Scheduled, selected-customer-Agent,
missing-connection, and dedicated protected-content cases are not event coverage.

After an environment is explicitly approved, use discovered model metadata rather
than relying on legacy defaults. This is a reference invocation, not authorization
to run while acceptance is blocked:

```powershell
$matrix = ".agents\skills\agent-insights-onboarding\scripts\agent_insights_live_matrix.py"
$configuration = @(
  "--confirm-live",
  "--subscription-id", "<approved-disposable-subscription-id>",
  "--location", "<selected-region>",
  "--model-name", "<discovered-model-name>",
  "--model-version", "<discovered-model-version>",
  "--model-format", "<discovered-format>",
  "--model-sku", "<discovered-sku>",
  "--model-capacity", "<discovered-integer-capacity>"
)
python $matrix @configuration --cases all --output-dir "<fresh-output-directory>"
```

For explicitly requested fallback coverage, replace the case selection with
`--cases "scratch-prompt-oneoff,scratch-hosted-oneoff"`. `--output-dir` must not already
exist; omitting it uses a new execution-specific directory under
`.agent-insights/live-matrix/`. Execution generates an ID unless `--execution-id` is
provided. Never overwrite an earlier execution's evidence.

The optional `--ingestion-timeout-seconds` and `--insights-timeout-seconds` defaults
are 900 and 2400 respectively; both must be positive and finite. Model defaults are
implementation defaults, not proof of availability in the selected region.

The output contains `summary.json` (schema 2) and `cleanup-manifest.json` (schema 1).
Interpret fields separately:

- Technical completion has `execution_status: complete` and `status: review_pending`,
  not a quality-passed claim.
- Per-case onboarding receipts may use root `complete` or `review_pending`. The matrix
  accepts either subject to its other technical checks, not as proof that it performed
  AI assessment or collected human feedback.
- `quality_status` is `findings` or `review_pending`; structural findings are not
  concealed behind technical completion.
- `ai_review_status: not_performed_by_matrix` and
  `human_review_status: not_collected_by_matrix` distinguish this automated harness
  from actual conversational assessment and human feedback.
- Review expected/completed case counts, current case, stage, and cleanup status.
  Completed-case counts include finished complete/failed cases, not cancelled partial
  attempts. Interrupted, cancelled, incomplete, or failed work is not a passed matrix.
- Exit code 0 means technical execution and cleanup completed, even when quality
  findings exist. It is not human approval.

For resume, supply `--resume-summary "<previous-output-directory>\summary.json"` with
the same selected configuration and a **fresh output directory**. The prior summary
must be a sealed final summary whose own matrix-level `cleanup_status` is `complete`.
Resume checks exact cases/profile, source content (including dirty files, baselines,
rubric, locks, and configuration), review versions, and runtime. Mismatches fail.

Completed cases retain their original assertions, provenance, quality findings, and
reuse history, including poor-quality output. They are never retried to improve a
score. Noncompleted cases use fresh owned sample attempts; old Agents and traffic are
not replayed. This matrix retry policy is separate from participant `status` recovery.

A per-case cleanup failure does not downgrade completed sample execution. It remains
visible as `cleanup_error`. When the final matrix summary confirms manifest cleanup
is complete, resume carries that case's original run ID and evidence without generating
another Agent, traffic batch, or Insights run.

A running summary or one with recorded incomplete cleanup is not resumable.
Independent cleanup updates its manifest; it does **not** rewrite the prior summary
to make it eligible. Preserve the evidence rather than editing a sealed summary to
bypass the check.

Independent cleanup uses the exact execution manifest, never a prefix sweep:

```powershell
python $matrix --confirm-live `
  --cleanup-manifest "<original-output-directory>\cleanup-manifest.json" `
  --execution-id "<matching-execution-id>" `
  --subscription-id "<matching-subscription-id>"
```

The manifest identifies only observed, owned resource groups for that execution.
Cleanup verifies schema/digest, the original signed-in user and tenant, execution ID,
subscription, and live ownership. Already-absent recorded groups are idempotent;
unobserved or uncertain creation remains explicit incomplete cleanup, not guessed
deletion. A different runner identity or missing manifest is an explicit failure, not
successful skipped cleanup. Other active runs are not scanned. Do not use the removed
broad `--cleanup-only` path.

### Opt-in GitHub workflow

`.github/workflows/live-matrix.yml` uses a Windows self-hosted runner labeled
`agent-insights-live` and the `live-azure` environment. Configure the explicitly chosen
`AGENT_INSIGHTS_LIVE_SUBSCRIPTION_ID`, `AGENT_INSIGHTS_LIVE_LOCATION`, and discovered
`AGENT_INSIGHTS_LIVE_MODEL_NAME`/`AGENT_INSIGHTS_LIVE_MODEL_VERSION` values there.
The runner must have an interactive Azure CLI user session.

Manual dispatch accepts a `cases` string; its `all` default means the primary two
cases. In this workflow, `AGENT_INSIGHTS_LIVE_CASES` is runtime environment data derived
from that input, not another workflow input or a same-named repository-variable lookup.
The workflow's cron runs only when `AGENT_INSIGHTS_LIVE_ENABLED=true` is set.
This opt-in GitHub trigger is distinct from scheduled Insights and must not be enabled
as an incidental bug-bash step.

The workflow carries `cleanup-manifest.json` in an execution-specific artifact to its
independent cleanup job. That job must use the same creating user and verify ownership;
it cannot sweep another execution or participant run. Execution/artifact identity comes
from the creating job's `execution_id` and `manifest_artifact` outputs, so retrying only
cleanup still uses the original attempt. Another runner is acceptable only with the
same Azure user, tenant, and subscription. The exact manifest and separate summary
artifacts have 14-day retention; full review bundles are not uploaded. These operational
artifacts are not human quality approval.

## Actual skill acceptance and documentation sync

### Recorded pilot, not participant expectations

The recorded pilot used Copilot CLI 1.0.84-1 and a discovered `gpt-5.4-mini`
deployment (version `2026-03-17`). Each corrected successful case generated six healthy
plus five faulty requests, correlated all eleven, and completed one on-demand run.

| Sample | Returned insights | Concrete fix candidates | Recorded AI preliminary assessment |
| --- | --- | --- | --- |
| Prompt | 2 | 1 prompt change | `mixed` |
| Hosted | 1 | 0 code changes | `poor` |

These are observations from one pilot, not guaranteed output, expected scores, or
human approval. Keep these implementation/acceptance notes out of the participant
quick start so they do not anchor ratings of new results.

The initial failed attempts revealed missing Project MI telemetry access. Exact
component-scoped Monitoring Reader access resolved that dependency without enabling
scheduling or granting MI model inference. The client also had to normalize the public
`on_demand` trigger to its `manual` contract. Failed evidence was preserved, and the
corrected samples were separate attempts rather than traffic replay under an old run.

**Current acceptance state:** real Copilot CLI Prompt/Hosted runs have completed
through Azure execution and persisted AI review. Initial dependency-permission failures
were preserved, and corrected fresh sample attempts each generated exactly eleven
requests and one successful on-demand run. Actual overall human feedback and
authenticated Portal UI acceptance remain pending; the browser required sign-in.
Do not equate the recorded `mixed`/`poor` AI assessments with human approval.
This pilot does not establish readiness for every participant identity. Before
sharing an invitation, also check the prepared project's effective access using the
intended participant permissions rather than assuming an administrator's access applies.

### Repeatable acceptance procedure

After offline checks, exercise **two fresh Copilot CLI conversations** in an explicitly
approved prepared project: one Prompt sample and one Hosted sample.

For headless CLI acceptance, tool approval alone is not equivalent to path/URL approval.
ARM resource-ID arguments can be classified as absolute paths by the CLI. Configure
only the explicitly authorized test process's required permissions, preserving all
organization/content-exclusion and explicit deny rules. If permission is denied,
stop rather than encoding arguments or routing around the gate. Normal participants
can use the interactive CLI approval flow.

1. Use the installed Copilot CLI's documented interactive invocation and verify that
   it discovers and loads this project skill. Direct Python calls and simulated
   conversations are not substitutes.
2. Observe discovery, doctor, the frozen plan, early run directory/stage progress,
   owned sample creation, bounded traffic, ingestion, and exactly one manual run.
3. Check that the portal link appears on run admission. Read the persisted sanitized
   evidence, Copilot's preliminary assessment, and any quality/evidence findings.
4. A real human supplies one overall rating and comment, or an explicit
   unable-to-judge/deferred/no-comment response. If they do not respond, preserve
   pending feedback and report the acceptance gap.
5. Preserve assets while the participant reviews. Perform the printed scoped cleanup
   only after review or explicit authorization to end that run, and verify shared
   project infrastructure remains.
6. Synchronize README, skill prompts, commands, result fields, review states, recovery,
   and cleanup guidance with the observed behavior. Rerun affected focused checks.

Do not make extra runs to replace poor output with a better score. Record blockers,
quality weaknesses, and missing human steps honestly. Scrub logs and resource
identifiers before sharing; never upload complete review bundles by default.
