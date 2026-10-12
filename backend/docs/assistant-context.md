# Assistant input and workspace context

## Conversation input

Assistant messages must contain at least one non-whitespace character. Whitespace-only content is rejected for both user requests and assistant history with a 422 validation response before the assistant provider is called. Valid message content is passed through unchanged, including surrounding whitespace and Markdown. Content remains limited to 1–4000 characters; a conversation contains 1–12 messages and must end with a user message.

Invalid assistant requests do not create assistant-success events. These checks validate request shape only; they do not change provider behavior or conversation history semantics.

## Workspace context sent to the provider

The assistant receives summaries of at most 10 registered models, preserving the Store's newest-first order. Each summary includes only available string identity fields (`id`, `name`, `version`, `framework`, and `kind`) with per-field length caps, plus validated evaluation metrics. Evaluation scores are limited to finite values from 0 through 1; sample counts are nonnegative integers no greater than `2**63 - 1`. Unknown metadata, descriptions, images, malformed values, and raw recent inference rows are excluded.

Workspace counts include only nonnegative integers through `2**63 - 1`. Missing or invalid counts are omitted; unavailable or invalid median latency is represented as `null`. The context reports total, included, and omitted model counts for the current input snapshot. Large serialized model summaries are omitted from the end until the context fits.

The limit is 12,000 characters for the workspace JSON serialized with `ensure_ascii=True`, `allow_nan=False`, and `sort_keys=True`. This is a character budget for workspace JSON only, not a token budget or a limit on the complete chat request. The JSON remains data in the system message; this handling does not eliminate prompt-injection risk.

Cluster CPU/memory, live workload status, deployed endpoints, and drift measurements are unavailable. Resource guidance is provisional and cannot apply infrastructure changes. A high prediction count alone does not establish throughput, CPU utilization, or drift and is not grounds for automatic scaling.
