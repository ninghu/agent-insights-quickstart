# Troubleshooting

| Failure | Meaning | Action |
| --- | --- | --- |
| `unsupported_cloud` | Azure CLI is not using `AzureCloud`. | Switch to Azure public cloud; sovereign clouds are not supported in v1. |
| `feature_unavailable` | The public project endpoint returned the preview's not-enabled response. | Use an Agent Insights-allowlisted subscription. |
| `insufficient_preflight_permission` | The caller cannot perform a planned resource or role write. | Give the exact `admin_handoff` role commands to an Azure administrator, then rerun doctor and require `ready`; no writes occurred. |
| `ambiguous_app_insights_connection` | More than one project connection can satisfy the request. | Resolve the project configuration manually; the workflow never deletes or guesses. |
| `model_unavailable` | The selected model/version/SKU is unavailable or lacks quota in the region. | Select a model returned by `doctor`. |
| `role_propagation_timeout` | ARM accepted a role assignment but the data-plane probe still fails. | Keep the run directory and retry `status`; do not recreate the assignment. |
| `ingestion_timeout` | Expected trace IDs did not all reach Application Insights. | Preserve the receipt and retry `status`; do not replay traffic. |
| `insights_run_failed` | Agent Insights reached a terminal failed state. | Use the sanitized run error and IDs for support; do not report success. |
| `empty_insights` | The run succeeded but returned no first insight. | Preserve the traffic/run evidence for diagnosis; do not generate unplanned traffic. |
| `ownership_mismatch` | A resource with the planned name is not owned by this run. | Choose a new scratch suffix or inspect the existing resource. Never reuse or delete it. |

Receipts are under `.agent-insights/runs/<run-id>/`. They are safe to share only after
reviewing them for environment-specific resource identifiers. They intentionally omit
tokens, keys, headers, connection strings, and customer telemetry.
