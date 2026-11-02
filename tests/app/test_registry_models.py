"""The model gateway is built on the first model call, not with the registry:
the worker and the commands run without ANTHROPIC_API_KEY (ADR 0016)."""

import pytest

from jhunt.app import registry
from jhunt.platform.config import Settings
from jhunt.platform.models import Message, ModelConfigError, ModelRequest


def test_without_a_key_only_a_model_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings()  # type: ignore[call-arg]
    call = registry._lazy_models(settings)  # building it needs no key
    with pytest.raises(ModelConfigError, match="ANTHROPIC_API_KEY is not set"):
        call(ModelRequest("pack.draft", (Message("user", "hi"),), 10))
