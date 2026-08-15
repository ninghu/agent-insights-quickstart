# Insight generation model

Agent Insights is a reasoning-heavy synthesis workload. Recommend a current GPT-5-class
or newer model; do not recommend GPT-4-class or older models for production insight
generation.

Use this preference order:

1. `gpt-5.6-terra` when it is current and has quota. It is the preferred reviewed
   customer candidate, subject to the customer's latency and quota needs.
2. An existing current GPT-5+ deployment that the customer has already qualified.
3. `gpt-5.4` as the service regression baseline when available.
4. Another current GPT-5+ candidate, clearly described as not blanket-qualified solely
   by its model family.

Run `discover deployments` first for an existing project. If no GPT-5+ deployment
exists, run `discover models --location <region> --project-resource-id <id>`. The result
contains only current, chat/Responses-capable GPT-5+ models with quota, plus a suggested
deployment name and exact Azure CLI command.

Before running the command:

- show the model, version, SKU, capacity, account, and estimated cost implications;
- ask the user to confirm deployment;
- refuse to overwrite a deployment with different model metadata;
- if deployment permission is missing, provide the exact command to an Azure
  administrator;
- verify the resulting deployment before running Agent Insights.

Use capacity `30` by default. The bounded quickstart can make up to 22 model calls
within one minute because Prompt Agent tool calls have a continuation turn. Capacity
`10` can throttle this traffic and produce an incomplete run.
