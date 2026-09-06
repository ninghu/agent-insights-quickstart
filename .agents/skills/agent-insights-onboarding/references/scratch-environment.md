# Scratch environment

Scratch is an **explicit, non-primary fallback**, not the default when a prepared
project has a missing prerequisite. Ask the participant to choose it before collecting
Azure identifiers or planning new infrastructure. Use `--profile bug-bash --mode scratch`.

## Selection and provisioning

Select an approved disposable Agent Insights-enabled public-cloud subscription, a
supported region, and the fixed Prompt or Hosted sample. Discover current models and
quota using [model selection](model-selection.md); supply the selected model metadata
rather than relying on legacy CLI defaults.

Scratch creates one tagged resource group containing:

- Foundry account, project, and low-capacity model deployment
- Log Analytics workspace and workspace-based Application Insights component
- Account and project Application Insights connections
- Required exact-scope role assignments
- Either a Prompt Agent or a Python 3.13 source-code Hosted Agent

Model lifecycle, chat/Responses capability, SKU, quota, and required access are checked
before mutation. Scratch needs additional provisioning permissions and incurs
infrastructure costs. Review the exact plan, not a request for blanket Owner access.
Platform identities needed for Foundry/Hosted deployment remain; bug-bash does not add
roles solely for scheduled Insights.
It does grant the Project MI component-scoped Monitoring Reader required for service
telemetry reads on the one-off path; MI model-inference grants remain disabled.

The CLI requires `--subscription-id`, `--location`, and `--agent-type prompt|hosted`
for this mode. Use the discovered `--model-name`, `--model-version`, `--model-format`,
`--model-sku`, and `--model-capacity` values. Scratch creates a sample automatically:
do not pass `--create-sample-agent` or `--agent-name`. Never pass scheduled flags.
Run `doctor` with the same `--profile bug-bash` configuration before `onboard`.

## Sample traffic

The selected sample is an order-status assistant with a deterministic `lookup_order`
dependency and the same baseline as the prepared-project path: **six healthy plus five
faulty requests**. Healthy requests remain the majority. Prompt traffic is sequential;
Hosted concurrency is at most two. Model and tool calls have no retries. Do not add
scenarios, repair the injected defect, or run both samples automatically.

The CLI records response, session, and trace IDs and polls Application Insights until
every expected root is correlated to the exact Agent/version. It never sends extra
traffic to compensate for delayed ingestion or poor insight quality.

## One-off result, review, and cleanup

After ingestion, bug-bash submits one manual Insights run. Show the run directory early
and the portal link as soon as the run is admitted. A successful service result with
valid review provenance initially becomes `review_pending`, with explicit quality
findings even for empty/prose-only output.
Follow [quality review](quality-review.md) for Copilot's preliminary assessment and one
overall human rating/comment.

Once current AI and rated overall human records exist, the CLI synchronizes the
receipt to `complete`. This is recording completion, not quality approval; an explicit
unable/deferred response must not be replaced with an invented numeric score.

Resources stay available during review. After review or explicit authorization to end
an incomplete run, use the printed cleanup command. Cleanup verifies the exact
resource-group ID, `created-by`, run ID, and initiating object ID before deletion.
Preserve partial journals if provisioning failed; never substitute broad tag/prefix
cleanup or replay traffic.

Legacy standard-profile scratch behavior remains supported underneath. It is not
selected by the bug-bash skill or included as a scheduled event scenario.
