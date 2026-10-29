"""OpenAI Responses API adapter, over plain httpx (ADR 0003).

build_body, parse_reply and map_error are pure. call is the thin layer that
sends the request.
"""

import httpx
from pydantic import SecretStr

from jhunt.platform.models.http import post_json, redact
from jhunt.platform.models.types import ModelCallError, ModelRequest, Reply, Stop, Usage

PROVIDER = "openai"
# The Responses API, not Chat Completions. Checked against
# developers.openai.com/api/reference on 2026-10-29. No version header: the API
# has one REST version.
URL = "https://api.openai.com/v1/responses"

# A 429 with one of these is a billing or quota limit, not a rate limit:
# retrying won't help until someone adds credit or raises a limit.
_NOT_RETRYABLE_429 = {
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}
# Provider text is dropped for auth errors: OpenAI's 401 quotes part of the key.
_NO_MESSAGE = {401, 403}
_MESSAGE_LIMIT = 200


def build_body(model: str, request: ModelRequest) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "input": [{"role": m.role, "content": m.text} for m in request.messages],
        "max_output_tokens": request.max_output_tokens,
        # OpenAI keeps responses by default; we never want our prompts stored.
        "store": False,
    }
    if request.system is not None:
        body["instructions"] = request.system
    return body


def parse_reply(body: object) -> Reply:
    """Raises AttributeError, KeyError or TypeError on a body that isn't a Responses response."""
    if not isinstance(body, dict):
        raise TypeError("response body is not a JSON object")
    texts: list[str] = []
    refused = False
    # Only message items carry the answer; reasoning and other items are skipped.
    for item in body["output"]:
        if item["type"] != "message":
            continue
        for part in item["content"]:
            if part["type"] == "output_text":
                texts.append(part["text"])
            elif part["type"] == "refusal":
                refused = True

    usage = body["usage"]
    # OpenAI's input_tokens already includes cached tokens, so no sum is needed.
    details = usage.get("input_tokens_details") or {}
    return Reply(
        text="".join(texts),
        stop=_stop(body, refused),
        usage=Usage(
            input_tokens=usage["input_tokens"],
            cache_read_tokens=details.get("cached_tokens") or 0,
            # Listed in the API reference; absent means no cache write.
            cache_write_tokens=details.get("cache_write_tokens") or 0,
            output_tokens=usage["output_tokens"],
        ),
    )


def _stop(body: dict[str, object], refused: bool) -> Stop:
    if refused:
        return "refusal"
    status = body.get("status")
    if status == "completed":
        return "end"
    incomplete = body.get("incomplete_details")
    if (
        status == "incomplete"
        and isinstance(incomplete, dict)
        and incomplete.get("reason") == "max_output_tokens"
    ):
        return "max_tokens"
    return "other"


def map_error(status: int, body: object, request_id: str | None) -> ModelCallError:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {}
    kinds = {error.get("type"), error.get("code")}
    retryable = status in (408, 429) or status >= 500
    if status == 429 and kinds & _NOT_RETRYABLE_429:
        retryable = False  # a billing or quota limit, not a rate limit
    message = "" if status in _NO_MESSAGE else str(error.get("message", ""))[:_MESSAGE_LIMIT]
    # error.code is the specific cause (e.g. credit_balance_exhausted); type is broader.
    error_type = error.get("code") or error.get("type") or f"http_{status}"
    return ModelCallError(PROVIDER, status, str(error_type), message, request_id, retryable)


def call(
    client: httpx.Client, api_key: SecretStr, model: str, request: ModelRequest
) -> tuple[Reply, str | None]:
    """Send one request. Returns the reply and the provider's request id."""
    secret = api_key.get_secret_value()
    headers = {"authorization": f"Bearer {secret}"}
    result = post_json(client, PROVIDER, URL, headers, build_body(model, request))
    request_id = result.headers.get("x-request-id")
    if result.status != 200:
        raise redact(map_error(result.status, result.body, request_id), secret)
    try:
        return parse_reply(result.body), request_id
    except (AttributeError, KeyError, TypeError) as exc:
        raise ModelCallError(
            PROVIDER, result.status, "invalid_response", type(exc).__name__, request_id, False
        ) from None
