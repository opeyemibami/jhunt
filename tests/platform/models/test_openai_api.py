"""OpenAI adapter: pure mapping functions, then the HTTP layer on MockTransport."""

import json

import httpx
import pytest
from pydantic import SecretStr

from jhunt.platform.models import openai_api
from jhunt.platform.models.types import Message, ModelCallError, ModelRequest, Reply, Usage

KEY = SecretStr("sk-proj-test-key-0123456789abcdef")
REQUEST = ModelRequest(
    step="scout.score",
    messages=(Message("user", "hi"), Message("assistant", "hello"), Message("user", "again")),
    max_output_tokens=50,
    system="be brief",
)


def message_item(*parts: dict[str, object]) -> dict[str, object]:
    return {"type": "message", "role": "assistant", "status": "completed", "content": list(parts)}


def ok_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "incomplete_details": None,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            message_item(
                {"type": "output_text", "text": "Hello", "annotations": []},
                {"type": "output_text", "text": " there", "annotations": []},
            ),
        ],
        "usage": {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 100},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 127,
        },
    }
    body.update(overrides)
    return body


# --- pure functions ---


def test_build_body() -> None:
    assert openai_api.build_body("gpt-6-luna", REQUEST) == {
        "model": "gpt-6-luna",
        "input": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ],
        "max_output_tokens": 50,
        "store": False,
        "instructions": "be brief",
    }


def test_build_body_omits_absent_system() -> None:
    body = openai_api.build_body("m", ModelRequest("s", (Message("user", "hi"),), 5))
    assert set(body) == {"model", "input", "max_output_tokens", "store"}


def test_parse_reply_joins_output_text_and_keeps_input_total() -> None:
    reply = openai_api.parse_reply(ok_body())
    assert reply.text == "Hello there"
    assert reply.stop == "end"
    assert reply.usage == Usage(
        input_tokens=120, cache_read_tokens=100, cache_write_tokens=0, output_tokens=7
    )


def test_parse_reply_reads_cache_writes_when_reported() -> None:
    usage = {
        "input_tokens": 50,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 40},
        "output_tokens": 1,
    }
    assert openai_api.parse_reply(ok_body(usage=usage)).usage == Usage(50, 0, 40, 1)


def test_parse_reply_without_details() -> None:
    usage = {"input_tokens": 3, "output_tokens": 4}
    assert openai_api.parse_reply(ok_body(usage=usage)).usage == Usage(3, 0, 0, 4)


def test_refusal_part_maps_to_refusal() -> None:
    output = [message_item({"type": "refusal", "refusal": "I can't help with that."})]
    reply = openai_api.parse_reply(ok_body(output=output))
    assert (reply.text, reply.stop) == ("", "refusal")


@pytest.mark.parametrize(
    ("status", "incomplete", "stop"),
    [
        ("completed", None, "end"),
        ("incomplete", {"reason": "max_output_tokens"}, "max_tokens"),
        ("incomplete", {"reason": "content_filter"}, "other"),
        ("incomplete", None, "other"),
        ("failed", None, "other"),
    ],
)
def test_parse_reply_status(status: str, incomplete: object, stop: str) -> None:
    body = ok_body(status=status, incomplete_details=incomplete)
    assert openai_api.parse_reply(body).stop == stop


def error_body(message: str = "why", **error: object) -> dict[str, object]:
    return {"error": {"message": message, "param": None, **error}}


@pytest.mark.parametrize(
    ("status", "error", "error_type", "retryable"),
    [
        (400, {"type": "invalid_request_error", "code": None}, "invalid_request_error", False),
        (
            404,
            {"type": "invalid_request_error", "code": "model_not_found"},
            "model_not_found",
            False,
        ),
        (429, {"type": "requests", "code": "rate_limit_exceeded"}, "rate_limit_exceeded", True),
        (429, {"type": "rate_limit_error", "code": "slow_down"}, "slow_down", True),
        (429, {"type": "insufficient_quota", "code": None}, "insufficient_quota", False),
        (
            429,
            {"type": "insufficient_quota", "code": "credit_balance_exhausted"},
            "credit_balance_exhausted",
            False,
        ),
        (
            429,
            {"type": "insufficient_quota", "code": "project_spend_limit_exceeded"},
            "project_spend_limit_exceeded",
            False,
        ),
        (408, {"type": "timeout", "code": None}, "timeout", True),
        (500, {"type": "server_error", "code": None}, "server_error", True),
        (
            503,
            {"type": "service_unavailable_error", "code": None},
            "service_unavailable_error",
            True,
        ),
    ],
)
def test_map_error(status: int, error: dict[str, object], error_type: str, retryable: bool) -> None:
    err = openai_api.map_error(status, error_body(**error), "req_1")
    assert (err.status, err.error_type, err.retryable, err.request_id, err.message) == (
        status,
        error_type,
        retryable,
        "req_1",
        "why",
    )


@pytest.mark.parametrize("status", [401, 403])
def test_map_error_drops_auth_error_text(status: int) -> None:
    body = error_body("Incorrect API key provided: sk-proj-****cdef.", code="invalid_api_key")
    assert openai_api.map_error(status, body, None).message == ""


def test_map_error_non_json_body() -> None:
    err = openai_api.map_error(502, None, None)
    assert (err.error_type, err.retryable, err.message) == ("http_502", True, "")


# --- HTTP layer ---


def call_with(handler: httpx.MockTransport) -> tuple[Reply, str | None]:
    return openai_api.call(httpx.Client(transport=handler), KEY, "gpt-6-luna", REQUEST)


def test_call_sends_bearer_key_and_reads_request_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ok_body(), headers={"x-request-id": "req_abc"})

    reply, request_id = call_with(httpx.MockTransport(handler))

    assert (reply.text, request_id) == ("Hello there", "req_abc")
    sent = seen[0]
    assert str(sent.url) == "https://api.openai.com/v1/responses"
    assert sent.headers["authorization"] == f"Bearer {KEY.get_secret_value()}"
    assert json.loads(sent.content)["store"] is False


def test_401_echoing_part_of_the_key_leaks_nothing() -> None:
    secret = KEY.get_secret_value()
    # OpenAI's real 401 shows the start and end of the key.
    echoed = f"Incorrect API key provided: {secret[:8]}****{secret[-4:]}. Full: {secret}"

    def handler(request: httpx.Request) -> httpx.Response:
        body = error_body(echoed, type="invalid_request_error", code="invalid_api_key")
        return httpx.Response(401, json=body, headers={"x-request-id": "req_401"})

    with pytest.raises(ModelCallError) as caught:
        call_with(httpx.MockTransport(handler))

    err = caught.value
    assert (err.status, err.error_type, err.retryable, err.request_id) == (
        401,
        "invalid_api_key",
        False,
        "req_401",
    )
    for fragment in (secret, secret[:8], secret[-4:]):
        assert fragment not in str(err)
        assert fragment not in repr(err)


def test_call_error_scrubs_an_echoed_key() -> None:
    echoed = f"bad request near {KEY.get_secret_value()}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=error_body(echoed, type="invalid_request_error"))

    with pytest.raises(ModelCallError) as caught:
        call_with(httpx.MockTransport(handler))
    assert KEY.get_secret_value() not in str(caught.value)
    assert "[redacted]" in str(caught.value)


@pytest.mark.parametrize("body", [b"not json", b'{"output": []}', b'{"output": [], "usage": null}'])
def test_call_unexpected_200_body_is_invalid_response(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"x-request-id": "req_x"})

    with pytest.raises(ModelCallError) as caught:
        call_with(httpx.MockTransport(handler))
    assert (caught.value.error_type, caught.value.retryable, caught.value.request_id) == (
        "invalid_response",
        False,
        "req_x",
    )
