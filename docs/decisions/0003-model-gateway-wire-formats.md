# 0003: Model gateway wire formats

Status: accepted (owner, 2026-10-29)

## Context

All model calls go through `jhunt.platform.models` (AGENTS.md hard rule 2), using plain httpx against each provider's REST API, with no SDKs. The design says which providers and that routes live in config. It does not say which API version, which endpoints, or how to make usage numbers comparable across providers. These choices change what we send and what we record, so they are written down here.

Everything below was checked against the providers' own docs on 2026-10-29.

## Decision

**Routes.** `<JHUNT_CONFIG_DIR>/model_routes.toml` has one `[routes]` table: `default` plus per-step keys such as `"scout.score"`, each `"provider:model"`.

- `JHUNT_CONFIG_DIR` is required and has no default, so the app never guesses its config location from the working directory.
- `MODEL_ROUTES` (JSON) overrides single keys, for local experiments only.
- Everything is validated when the gateway starts:
  - a missing directory or file;
  - invalid TOML or JSON;
  - an unknown table;
  - a malformed step key or route;
  - an unknown provider;
  - a missing `default`;
  - a route whose provider has no API key.

**Anthropic.** `POST https://api.anthropic.com/v1/messages` with `anthropic-version: 2026-10-21`, the latest version, which Anthropic recommends.

- That version rejects `temperature`, `top_p` and `top_k`, so we never send them.
- We send no `thinking` field, so current models use their default adaptive thinking.
- The reply is the concatenated `text` blocks; thinking blocks are ignored.
- The request id is the `request-id` response header.

**OpenAI.** The Responses API, `POST https://api.openai.com/v1/responses`, with `Authorization: Bearer <key>`. Chat Completions is not used: Responses is OpenAI's recommended API, and the Assistants API it replaced shut down on 2026-08-26. There is no version header to pin.

- We send `store: false`, so OpenAI does not keep our prompts; the API's default is to store them.
- The system prompt goes in `instructions`, and the messages go in `input` as `{role, content}`.
- The reply is the concatenated `output_text` parts of `message` items. Reasoning and other items are skipped.
- `stop` is:
  - `refusal` if any part is a `refusal`;
  - `end` when `status` is `completed`;
  - `max_tokens` when `status` is `incomplete` with `incomplete_details.reason` = `max_output_tokens`;
  - `other` otherwise, including `content_filter` and `failed`.
- The request id is the `x-request-id` response header.

**Usage.** Each call is recorded as provider, model, step, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, latency and request id.

- `input_tokens` is every input token, cached or not. That is OpenAI's definition, so OpenAI's value is used as is. Anthropic's `input_tokens` excludes cached tokens, so the adapter adds `cache_read_input_tokens` and `cache_creation_input_tokens` back.
- Cache counts come from Anthropic's two cache fields, and from OpenAI's `input_tokens_details.cached_tokens` and `input_tokens_details.cache_write_tokens`. Either OpenAI field counts as 0 when absent. The reference lists `cache_write_tokens`, but it doesn't say whether `input_tokens` includes it; we assume it does, like `cached_tokens`.
- The cache counts are kept separately as well, because cached tokens are priced differently and week 16 cost tracking needs them apart.

**Errors.**

- A failed call raises `ModelCallError`, carrying the status, the provider's error type, the request id and `retryable`.
- Retryable means 408, 429 or 5xx (Anthropic's 529 included), or no HTTP response at all.
- One exception for OpenAI: a 429 whose `error.type` or `error.code` is a billing or quota limit is not retryable, because retrying won't help until someone adds credit or raises a limit. The codes are `insufficient_quota`, `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded` and `organization_usage_limit_exceeded`.
- OpenAI errors report `error.code` as the error type when present, and `error.type` otherwise; the code is the more specific cause.
- Nothing retries yet; retries, budgets and fallback come in week 16.
- The provider's error text is dropped for 401 and 403, because OpenAI's 401 quotes the start and end of the key. Other error text is cut to 200 characters, and any copy of the key is replaced with `[redacted]`.

**Logging.**

- Each call, success or failure, logs one JSON line (`"event": "model_call"`) with the record fields and no prompt or response text.
- A `model_calls` table comes with observability.

**I/O boundary.** Each adapter keeps building the request, parsing the response and mapping errors as pure functions. Only `http.py` and each adapter's small `call` function touch the network, so a move to async httpx stays inside those.

## Consequences

- Version and endpoint changes are deliberate edits to an adapter constant, with this ADR updated.
- Cheapest current models, used by the live tests: `claude-haiku-5-5` and `gpt-6-luna`. `gpt-5-nano` is cheaper but shuts down on 2026-12-11.
- Live tests are marked `live` and run only with `RUN_LIVE_MODEL_TESTS=1`. They are the only check against the real APIs; the unit tests use `httpx.MockTransport`.
