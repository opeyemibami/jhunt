"""One real call to Anthropic. Skipped unless RUN_LIVE_MODEL_TESTS=1.

Uses the cheapest current model (claude-haiku-5-5, checked 2026-10-29) and costs
well under $0.001. Needs ANTHROPIC_API_KEY in the environment.
"""

import logging
import os

import pytest

from jhunt.platform.config import Settings
from jhunt.platform.models import Message, ModelGateway, ModelRequest

pytestmark = pytest.mark.live


def test_one_tiny_call(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)  # real httpx and httpcore loggers this time
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    assert key, "RUN_LIVE_MODEL_TESTS=1 needs ANTHROPIC_API_KEY"
    settings = Settings(  # type: ignore[call-arg]
        model_routes='{"default": "anthropic:claude-haiku-5-5"}'
    )

    gateway = ModelGateway.from_settings(settings)
    try:
        response = gateway.call(
            ModelRequest(
                step="live.check",
                messages=(Message("user", "Reply with the single word: ok"),),
                # Room for adaptive thinking before the one-word answer.
                max_output_tokens=512,
            )
        )
    finally:
        gateway.close()

    record = response.record
    assert response.stop == "end"
    assert "ok" in response.text.lower()
    assert record.input_tokens > 0
    assert record.output_tokens > 0
    assert record.request_id is not None
    assert record.request_id.startswith("req_")
    for text in (caplog.text, *capsys.readouterr()):
        assert key not in text
