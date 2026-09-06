# Agent Insights Quickstart

Join the **Microsoft Foundry Agent Insights quality bug bash** with **Copilot CLI**.
Copilot creates a sample Agent in your approved test project, generates a small batch
of requests, and runs Insights once. It gives a preliminary assessment; you review the
result and provide **one overall rating and comment**.

**The goal is to evaluate insight quality, not just get a successful API response.**
Missing root causes, misleading findings, and weak fixes are useful bug-bash feedback.

> [!IMPORTANT]
> This is a community preview sample, not an official Microsoft support channel.
> Your subscription must already be Agent Insights-enabled. Only Azure public cloud
> is supported. Use an approved test project, not customer Agents or production data.

## Before you start

| Have ready | What you need |
| --- | --- |
| Tools | Git, a current **Copilot CLI** signed in with an account that can use it, Python **3.13+**, and Azure CLI **2.80+**. |
| Azure login | An interactive Azure CLI **user** login for the project's tenant. Service-principal/OIDC login is not this participant flow. |
| Test project | The **Foundry project endpoint** from your organizer, or an approved existing test project you have prepared yourself. |
| Project readiness | A suitable model deployment with quota, one usable Application Insights connection, and the necessary caller and project-identity access. |
| Event instructions | The organizer's preferred sample/model, feedback channel, and resource-retention/cleanup instructions. |

An endpoint has this shape; this is a format example, **not a usable test endpoint**:

```text
https://<account>.services.ai.azure.com/api/projects/<project>
```

You do **not** need to bring an existing Agent, API keys, Docker, or an Azure Container
Registry. Copilot creates a fresh sample and guides any missing-tool setup. Python
dependencies belong in an ignored virtual environment, not the system interpreter.

Ask the organizer to resolve missing project prerequisites before you start. In
particular, the current Insights service needs **Project MI Monitoring Reader on the
connected Application Insights component**, even for one-off telemetry reads. Model
access uses the caller's delegated identity. You should not need subscription-wide
Owner access just to participate.

Organizers: use the [readiness and invitation checklist](.agents/skills/agent-insights-onboarding/references/organizer-guide.md).
Detailed roles are in the [permission reference](.agents/skills/agent-insights-onboarding/references/permissions.md).
If you have no ready project, arrange one with the organizer; scratch provisioning is
an explicitly chosen [fallback](.agents/skills/agent-insights-onboarding/references/scratch-environment.md),
not the default participant path.

## Quick start

Clone the repository and start an **interactive** Copilot CLI session inside it:

```shell
git clone https://github.com/ninghu/agent-insights-quickstart
cd agent-insights-quickstart
copilot
```

If needed, sign in using `copilot login` and `az login`. Then paste:

```text
Run the Agent Insights quality bug bash using the agent-insights-onboarding skill.
Use an existing test Foundry project and create a fresh sample Agent.
Ask me for the project endpoint and sample choice, then guide me through
the preliminary AI review and my overall feedback.
```

Confirm that Copilot **loads `agent-insights-onboarding`** before allowing Azure changes.
The skill is included at `.agents/skills/agent-insights-onboarding`; no separate skill
download is needed for this checkout. If it is not found, run `copilot skill list` from
the repository folder and restart a fresh CLI session there. Do not substitute ad-hoc
Azure commands for a skill that did not load.

Copilot will guide you through:

1. **Choose the project and sample.** Supply the endpoint and choose Prompt or Hosted.
   Copilot discovers the resource IDs and model deployment; it does not guess a
   subscription from your current login.
2. **Check readiness.** The read-only doctor reports missing access, quota, or
   connections. If blocked, give the exact handoff to the organizer instead of
   granting yourself broader access.
3. **Run the sample once.** Review the selected scope and plan, then approve the
   intended tool/URL/path requests in the interactive session. Copilot creates a new
   owned sample, sends **six healthy plus five faulty requests**, and submits one
   **manual, one-off** Insights run. There is no scheduling choice.
4. **Open the result.** Keep the printed run directory. Open the Foundry link as soon
   as it appears; the first Insights run may take **10-20 minutes**, plus deployment
   and ingestion. Sign in to the Portal if prompted; Azure CLI login does not sign
   your browser in automatically.
5. **Review and give feedback.** Copilot records its preliminary assessment, then asks
   for your overall rating and comment. Use the instructions below to share feedback
   and clean up when the review is finished.

Choosing one sample does not run both. If you are assigned both, finish the first
workflow and explicitly request a separate fresh sample run for the second.

## Two fixed samples

Both samples are order-status assistants with known, deliberately planted defects.
Use them as evaluation baselines; **do not fix the sample before running the bug bash**.

| Sample | Deliberately injected defect | What a useful insight should identify |
| --- | --- | --- |
| **Prompt Agent** | An instruction claims an order was delivered when the lookup tool failed. | The wrong failure-handling instruction, with a specific prompt correction that preserves healthy replies. |
| **Code-based Hosted Agent** | A 20 ms timeout is shorter than a simulated 80 ms lookup. | The timeout misconfiguration, with a specific source/configuration correction rather than generic retry advice. |

Each sample has six normal and five faulty requests; the five faults exercise one
known cause with different sample inputs. These are not randomized Agents or eleven
different defects. Hosted uses the bundled Python source and remote build; you do not
need to build a container yourself.

## Review and send feedback

Open **Monitor > Agent Insights** for your sample if the link lands on the project
home. Review the actual insights and proposed changes, not only Copilot's summary:

- Did the insight find the **real cause**, or just repeat a symptom?
- Does its **evidence** support the claim? Are healthy requests incorrectly flagged?
- Is the proposed fix **specific and actionable**, and does it preserve healthy behavior?
- Is anything missing, duplicated, misleading, or unsupported?

Copilot's judgment is **preliminary**, and a nonempty diff is not a proven fix. No fix
is automatically applied. Do not rerun traffic or generate another Insights result
just to obtain a higher score.

Give **one overall score for the whole result** and a comment:
**1 poor, 2 mostly weak, 3 mixed, 4 useful, 5 highly useful**.
You can also say **unable to judge**, **defer**, or **no comment**. A low rating is
valuable; you are not expected to make the sample "pass." No per-insight human grading
is required. See the [detailed rubric](.agents/skills/agent-insights-onboarding/references/quality-review.md).

**Recording feedback is local; it is not submission to the organizer.** Receipts and
reports are saved under `.agent-insights/runs/<run-id>/`, including `quality-report.md`,
`ai-review.json`, and any actual `human-review.json`. Follow the organizer's feedback
channel to share a sanitized summary, including when no bug was found.

| What to report | Where |
| --- | --- |
| Incorrect/missing insights or weak suggested fixes | [Agent Insights service bug form](https://msdata.visualstudio.com/Vienna/_workitems/create/Bug?templateId=6d5d4dfe-fd55-45f3-b9c9-f7cc2b0e1835&ownerId=5d069bfc-f7ae-4d93-bee7-c94d439a26a7) (requires access to the internal project). |
| A problem with this repository, skill, or CLI workflow | [Repository bug form](https://github.com/ninghu/agent-insights-quickstart/issues/new?template=bug.yml). |
| Overall rating/comment or an inaccessible bug form | The feedback route supplied by your organizer. Nothing is uploaded automatically. |

For a useful report, include the sample, repository commit/release, reproduction steps,
expected versus actual behavior, and the error code/stage if applicable. Label AI
assessment and your own rating/comment separately. Ask Copilot to help draft a sanitized
report, but review it before submitting.

**Do not upload the entire run directory or raw telemetry.** Remove tokens, keys,
connection strings, authorization headers, and environment-specific identifiers before
sharing outside an approved internal channel.
See the [feedback checklist](.agents/skills/agent-insights-onboarding/references/quality-review.md#feedback-material).

## If something goes wrong

| Situation | Safe next step |
| --- | --- |
| Skill not loaded | Check `copilot skill list` from this checkout and use a fresh interactive session. |
| Tool permission denied | Review the interactive approval request for the intended project. Tool, URL, and path permissions are separate; do not bypass explicit denies or change global policy. |
| Doctor or service returns `Forbidden` | Keep the exact code/stage and ask the organizer to check caller access **and** Project MI telemetry access. Do not enable scheduling or grant Owner as a workaround. |
| Ingestion or polling times out | Keep the run directory and ask Copilot to resume that same run with `status`, without replaying traffic. Terminal or partial-traffic failures need diagnosis, not an automatic replacement run. |
| Empty insights or no concrete fix | Report the quality finding. Successful execution does not guarantee good insights. |
| `review_pending` | Read the detailed review status: AI or human feedback may still be pending. Explicit defer/unable-to-judge is valid feedback, not something to replace with an invented score. |

The [troubleshooting guide](.agents/skills/agent-insights-onboarding/references/troubleshooting.md)
covers the detailed codes and recovery rules. An empty search result does not prove a
run is absent: its directory is git-ignored.

## Clean up your run

Keep the result available until you and the organizer have finished reviewing it.
Model, monitoring, and Hosted Agent charges can continue while resources remain;
reported analysis estimates are not the total environment bill.

Then ask Copilot to use **the cleanup command printed for your own sample run**.
It checks the receipt and live ownership. Do not delete the shared Foundry project,
model, monitoring resources, or resource group, and do not use another person's receipt.
An organizer's infrastructure-preparation cleanup command can delete the entire test RG;
it is **not** a participant cleanup shortcut.

If cleanup fails, keep the receipt and report the blocker to the organizer. Do not
fall back to broad name-prefix or tag-based deletion.

## Organizers and contributors

Participants do not need the technical live matrix, direct Azure provisioning commands,
or development dependencies to follow this guide.

- [Organizer readiness and invitation checklist](.agents/skills/agent-insights-onboarding/references/organizer-guide.md)
- [Roles and access](.agents/skills/agent-insights-onboarding/references/permissions.md)
- [Scratch fallback](.agents/skills/agent-insights-onboarding/references/scratch-environment.md)
- [Developer commands, live matrix, and acceptance evidence](CONTRIBUTING.md)

The skill uses the `bug-bash` profile. The standalone CLI's default `standard` profile
retains legacy capabilities; those are not steps for a participant to run.

## License

[MIT](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for adapted public
sample patterns.
