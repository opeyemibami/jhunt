"""The gateway: startup checks, one JSON log line per call, and no key leaks."""

import json
import logging
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from jhunt.platform.config import Settings
from jhunt.platform.models import ModelCallError, ModelConfigError, ModelGateway
from jhunt.platform.models.gateway import ADAPTERS, KEY_VARIABLES
from jhunt.platform.models.routes import PROVIDERS
from jhunt.platform.models.types import Message, ModelRequest

FAKE_KEY = "sk-ant-fake-key-must-never-print-0123456789"
PROMPT = "secret-prompt-text"
ANSWER = "secret-answer-text"
REQUEST = ModelRequest("scout.score", (Message("user", PROMPT),), 20, system=PROMPT)


def make_settings(config_dir: Path, key: str | None = FAKE_KEY) -> Settings:
    (config_dir / "model_routes.toml").write_text(
        '[routes]\ndefault = "anthropic:claude-haiku-5-5"\n'
    )
    return Settings(
        postgres_user="u",
        postgres_password=SecretStr("p"),
        postgres_db="d",
        postgres_host="h",
        postgres_port=1,
        jhunt_config_dir=config_dir,
        anthropic_api_key=SecretStr(key) if key is not None else None,
        model_routes=None,
    )


def gateway_with(tmp_path: Path, response: httpx.Response) -> ModelGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ModelGateway.from_settings(make_settings(tmp_path), client)


def ok_response() -> httpx.Response:
    body = {
        "content": [{"type": "text", "text": ANSWER}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "output_tokens": 4},
    }
    return httpx.Response(200, json=body, headers={"request-id": "req_ok"})


def model_call_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "jhunt.platform.models.gateway"
    ]


def test_every_provider_has_an_adapter_and_a_key_variable() -> None:
    assert set(ADAPTERS) == set(PROVIDERS) == set(KEY_VARIABLES)


def test_missing_key_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ModelConfigError, match="ANTHROPIC_API_KEY is not set"):
        ModelGateway.from_settings(make_settings(tmp_path, key=None))


def test_call_returns_text_and_record(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    response = gateway_with(tmp_path, ok_response()).call(REQUEST)

    assert (response.text, response.stop) == (ANSWER, "end")
    record = response.record
    assert (record.provider, record.model, record.step, record.request_id) == (
        "anthropic",
        "claude-haiku-5-5",
        "scout.score",
        "req_ok",
    )
    assert (record.input_tokens, record.cache_read_tokens, record.output_tokens) == (5, 2, 4)
    assert record.latency_ms >= 0


def test_one_json_log_line_per_call_without_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    gateway_with(tmp_path, ok_response()).call(REQUEST)

    [line] = model_call_lines(caplog)
    assert line == {
        "event": "model_call",
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "step": "scout.score",
        "input_tokens": 5,
        "cache_read_tokens": 2,
        "cache_write_tokens": 0,
        "output_tokens": 4,
        "latency_ms": line["latency_ms"],
        "request_id": "req_ok",
    }
    assert PROMPT not in caplog.text
    assert ANSWER not in caplog.text


def test_failed_call_logs_one_line_and_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    response = httpx.Response(429, json=body, headers={"request-id": "req_429"})

    with pytest.raises(ModelCallError):
        gateway_with(tmp_path, response).call(REQUEST)

    [line] = model_call_lines(caplog)
    assert (line["error_status"], line["error_type"], line["retryable"], line["request_id"]) == (
        429,
        "rate_limit_error",
        True,
        "req_429",
    )


def test_key_never_appears_in_reprs_errors_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)  # every logger, httpx included
    settings = make_settings(tmp_path)
    # An auth error whose message quotes the key, as some providers do.
    body = {"error": {"type": "authentication_error", "message": f"bad key {FAKE_KEY}"}}
    gateway = gateway_with(tmp_path, httpx.Response(401, json=body))

    with pytest.raises(ModelCallError) as caught:
        gateway.call(REQUEST)

    for text in (
        repr(settings),
        str(settings),
        repr(gateway),
        str(caught.value),
        repr(caught.value),
        caplog.text,
        *capsys.readouterr(),
    ):
        assert FAKE_KEY not in text
