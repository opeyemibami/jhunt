"""Anthropic adapter: pure mapping functions, then the HTTP layer on MockTransport."""

import json

import httpx
import pytest
from pydantic import SecretStr

from jhunt.platform.models import anthropic_api
from jhunt.platform.models.types import Message, ModelCallError, ModelRequest, Usage

KEY = SecretStr("sk-ant-test-key-0123456789")
REQUEST = ModelRequest(
    step="scout.score",
    messages=(Message("user", "hi"),),
    max_output_tokens=50,
    system="be brief",
)


def ok_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": " there"},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 5,
            "output_tokens": 7,
        },
    }
    body.update(overrides)
    return body


# --- pure functions ---


def test_build_body() -> None:
    assert anthropic_api.build_body("claude-haiku-5-5", REQUEST) == {
        "model": "claude-haiku-5-5",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "be brief",
    }


def test_build_body_omits_absent_system_and_never_sends_sampling_params() -> None:
    body = anthropic_api.build_body("m", ModelRequest("s", (Message("user", "hi"),), 5))
    assert set(body) == {"model", "max_tokens", "messages"}


def test_parse_reply_joins_text_blocks_and_sums_input() -> None:
    reply = anthropic_api.parse_reply(ok_body())
    assert reply.text == "Hello there"
    assert reply.stop == "end"
    assert reply.usage == Usage(
        input_tokens=115, cache_read_tokens=100, cache_write_tokens=5, output_tokens=7
    )


def test_parse_reply_without_cache_fields() -> None:
    reply = anthropic_api.parse_reply(ok_body(usage={"input_tokens": 3, "output_tokens": 4}))
    assert reply.usage == Usage(3, 0, 0, 4)


@pytest.mark.parametrize(
    ("raw", "stop"),
    [
        ("end_turn", "end"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
        ("tool_use", "other"),
        ("pause_turn", "other"),
        (None, "other"),
    ],
)
def test_parse_reply_stop_reasons(raw: str | None, stop: str) -> None:
    assert anthropic_api.parse_reply(ok_body(stop_reason=raw)).stop == stop


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (400, "invalid_request_error", False),
        (404, "not_found_error", False),
        (429, "rate_limit_error", True),
        (500, "api_error", True),
        (504, "timeout_error", True),
        (529, "overloaded_error", True),
    ],
)
def test_map_error(status: int, error_type: str, retryable: bool) -> None:
    body = {"type": "error", "error": {"type": error_type, "message": "why"}, "request_id": "r1"}
    err = anthropic_api.map_error(status, body, None)
    assert (err.status, err.error_type, err.retryable, err.request_id, err.message) == (
        status,
        error_type,
        retryable,
        "r1",
        "why",
    )


@pytest.mark.parametrize("status", [401, 403])
def test_map_error_drops_auth_error_text(status: int) -> None:
    body = {"error": {"type": "authentication_error", "message": "key sk-ant-... is bad"}}
    assert anthropic_api.map_error(status, body, "r1").message == ""


def test_map_error_non_json_body() -> None:
    err = anthropic_api.map_error(502, None, "r1")
    assert (err.error_type, err.retryable, err.message) == ("http_502", True, "")


# --- HTTP layer ---


def client_for(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_call_sends_headers_and_reads_request_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ok_body(), headers={"request-id": "req_abc"})

    reply, request_id = anthropic_api.call(
        client_for(httpx.MockTransport(handler)), KEY, "claude-haiku-5-5", REQUEST
    )

    assert (reply.text, request_id) == ("Hello there", "req_abc")
    sent = seen[0]
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == KEY.get_secret_value()
    assert sent.headers["anthropic-version"] == "2026-10-21"
    assert json.loads(sent.content)["model"] == "claude-haiku-5-5"


def test_call_error_raises_with_request_id_and_scrubs_key() -> None:
    echoed = f"overloaded; you sent {KEY.get_secret_value()}"

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"type": "error", "error": {"type": "overloaded_error", "message": echoed}}
        return httpx.Response(529, json=body, headers={"request-id": "req_err"})

    with pytest.raises(ModelCallError) as caught:
        anthropic_api.call(client_for(httpx.MockTransport(handler)), KEY, "m", REQUEST)

    err = caught.value
    assert (err.status, err.request_id, err.retryable) == (529, "req_err", True)
    assert KEY.get_secret_value() not in str(err)
    assert "[redacted]" in str(err)


def test_call_timeout_is_retryable_without_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(ModelCallError) as caught:
        anthropic_api.call(client_for(httpx.MockTransport(handler)), KEY, "m", REQUEST)
    assert (caught.value.status, caught.value.error_type, caught.value.retryable) == (
        None,
        "timeout",
        True,
    )


@pytest.mark.parametrize(
    "body", [b"not json", b'{"content": []}', b"[]", b'{"content": [], "usage": null}']
)
def test_call_unexpected_200_body_is_invalid_response(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"request-id": "req_x"})

    with pytest.raises(ModelCallError) as caught:
        anthropic_api.call(client_for(httpx.MockTransport(handler)), KEY, "m", REQUEST)
    assert (caught.value.error_type, caught.value.retryable, caught.value.request_id) == (
        "invalid_response",
        False,
        "req_x",
    )
