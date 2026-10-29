"""Anthropic Messages API adapter, over plain httpx (ADR 0003).

build_body, parse_reply and map_error are pure. call is the thin layer that
sends the request.
"""

from collections.abc import Mapping

import httpx
from pydantic import SecretStr

from jhunt.platform.models.http import post_json, redact
from jhunt.platform.models.types import ModelCallError, ModelRequest, Reply, Stop, Usage

PROVIDER = "anthropic"
URL = "https://api.anthropic.com/v1/messages"
# Checked against platform.claude.com/docs/en/api/versioning on 2026-10-29.
# This version rejects temperature, top_p and top_k, so we never send them.
API_VERSION = "2026-10-21"

_STOPS: Mapping[object, Stop] = {
    "end_turn": "end",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}
# 429 rate limit, 5xx server errors, 529 overloaded. Everything else is our fault
# or the account's, and the same request will fail again.
_RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
# Provider text is dropped for auth errors, in case it quotes the credential.
_NO_MESSAGE = {401, 403}
_MESSAGE_LIMIT = 200


def build_body(model: str, request: ModelRequest) -> dict[str, object]:
    # No `thinking` field: current models default to adaptive thinking.
    body: dict[str, object] = {
        "model": model,
        "max_tokens": request.max_output_tokens,
        "messages": [{"role": m.role, "content": m.text} for m in request.messages],
    }
    if request.system is not None:
        body["system"] = request.system
    return body


def parse_reply(body: object) -> Reply:
    """Raises AttributeError, KeyError or TypeError on a body that isn't a Messages response."""
    if not isinstance(body, dict):
        raise TypeError("response body is not a JSON object")
    # Only text blocks; thinking blocks are not part of the answer.
    text = "".join(block["text"] for block in body["content"] if block["type"] == "text")
    usage = body["usage"]
    # Anthropic's input_tokens excludes cached tokens; add them back so
    # input_tokens means "all input" for every provider.
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    return Reply(
        text=text,
        stop=_STOPS.get(body.get("stop_reason"), "other"),
        usage=Usage(
            input_tokens=usage["input_tokens"] + cache_read + cache_write,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=usage["output_tokens"],
        ),
    )


def map_error(status: int, body: object, request_id: str | None) -> ModelCallError:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {}
    if request_id is None and isinstance(body, dict):
        request_id = body.get("request_id")
    message = "" if status in _NO_MESSAGE else str(error.get("message", ""))[:_MESSAGE_LIMIT]
    return ModelCallError(
        PROVIDER,
        status,
        str(error.get("type", f"http_{status}")),
        message,
        request_id,
        retryable=status in _RETRYABLE,
    )


def call(
    client: httpx.Client, api_key: SecretStr, model: str, request: ModelRequest
) -> tuple[Reply, str | None]:
    """Send one request. Returns the reply and the provider's request id."""
    headers = {"x-api-key": api_key.get_secret_value(), "anthropic-version": API_VERSION}
    result = post_json(client, PROVIDER, URL, headers, build_body(model, request))
    request_id = result.headers.get("request-id")
    if result.status != 200:
        raise redact(map_error(result.status, result.body, request_id), headers["x-api-key"])
    try:
        return parse_reply(result.body), request_id
    except (AttributeError, KeyError, TypeError) as exc:
        raise ModelCallError(
            PROVIDER, result.status, "invalid_response", type(exc).__name__, request_id, False
        ) from None
