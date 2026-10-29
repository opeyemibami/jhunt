"""The model gateway: the only way any jhunt code calls a model provider."""

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict

import httpx
from pydantic import SecretStr

from jhunt.platform.config import Settings
from jhunt.platform.models import anthropic_api, openai_api
from jhunt.platform.models.http import new_client
from jhunt.platform.models.routes import Routes, load_routes
from jhunt.platform.models.types import (
    CallRecord,
    ModelCallError,
    ModelConfigError,
    ModelRequest,
    ModelResponse,
    Reply,
)

log = logging.getLogger(__name__)

Adapter = Callable[[httpx.Client, SecretStr, str, ModelRequest], tuple[Reply, str | None]]
# Must cover routes.PROVIDERS; a test checks they match.
ADAPTERS: Mapping[str, Adapter] = {"anthropic": anthropic_api.call, "openai": openai_api.call}
KEY_VARIABLES = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class ModelGateway:
    def __init__(
        self, routes: Routes, keys: Mapping[str, SecretStr | None], client: httpx.Client
    ) -> None:
        # Fail at startup, not on the first call that needs the missing key.
        for provider in sorted(routes.providers()):
            if keys.get(provider) is None:
                raise ModelConfigError(
                    f"a route uses {provider} but {KEY_VARIABLES[provider]} is not set"
                )
        self._routes = routes
        self._keys = {p: k for p, k in keys.items() if k is not None}
        self._client = client

    @classmethod
    def from_settings(
        cls, settings: Settings, client: httpx.Client | None = None
    ) -> "ModelGateway":
        routes = load_routes(settings.jhunt_config_dir, settings.model_routes)
        keys = {"anthropic": settings.anthropic_api_key, "openai": settings.openai_api_key}
        return cls(routes, keys, client or new_client())

    def close(self) -> None:
        self._client.close()

    def call(self, request: ModelRequest) -> ModelResponse:
        route = self._routes.for_step(request.step)
        start = time.monotonic()
        try:
            reply, request_id = ADAPTERS[route.provider](
                self._client, self._keys[route.provider], route.model, request
            )
        except ModelCallError as exc:
            _log_call(
                provider=route.provider,
                model=route.model,
                step=request.step,
                latency_ms=_ms_since(start),
                request_id=exc.request_id,
                error_status=exc.status,
                error_type=exc.error_type,
                retryable=exc.retryable,
            )
            raise
        record = CallRecord(
            provider=route.provider,
            model=route.model,
            step=request.step,
            input_tokens=reply.usage.input_tokens,
            cache_read_tokens=reply.usage.cache_read_tokens,
            cache_write_tokens=reply.usage.cache_write_tokens,
            output_tokens=reply.usage.output_tokens,
            latency_ms=_ms_since(start),
            request_id=request_id,
        )
        _log_call(**asdict(record))
        return ModelResponse(text=reply.text, stop=reply.stop, record=record)


def _ms_since(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def _log_call(**fields: object) -> None:
    # One JSON line per call, success or failure. Ids and counts only: no
    # prompt or response text ever goes in here.
    log.info(json.dumps({"event": "model_call", **fields}, sort_keys=True))
