"""One request, reply and call record for every provider, plus the gateway's errors."""

from dataclasses import dataclass
from typing import Literal

Role = Literal["user", "assistant"]
# "end": the model finished. "max_tokens": cut off at max_output_tokens.
# "refusal": the provider or model declined. "other": anything else.
Stop = Literal["end", "max_tokens", "refusal", "other"]


@dataclass(frozen=True)
class Message:
    role: Role
    text: str


@dataclass(frozen=True)
class ModelRequest:
    step: str  # route key, e.g. "scout.score"; unknown steps use the default route
    messages: tuple[Message, ...]
    max_output_tokens: int
    system: str | None = None


@dataclass(frozen=True)
class Usage:
    # Every input token, cached or not, so the number compares across providers.
    input_tokens: int
    # The cached part of input_tokens, kept apart because it is priced differently.
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Reply:
    """What an adapter parses out of a provider's successful response."""

    text: str
    stop: Stop
    usage: Usage


@dataclass(frozen=True)
class CallRecord:
    """Logged once per call. Never holds prompt or response text."""

    provider: str
    model: str
    step: str
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    latency_ms: int
    request_id: str | None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    stop: Stop
    record: CallRecord


class ModelConfigError(Exception):
    """Routes, keys or config files are wrong. Raised at startup, before any call."""


class ModelCallError(Exception):
    """A provider call failed.

    status is None when no HTTP response arrived (timeout, connection error).
    retryable says whether the same request may succeed later; nothing retries
    yet (week 16).
    """

    def __init__(
        self,
        provider: str,
        status: int | None,
        error_type: str,
        message: str,
        request_id: str | None,
        retryable: bool,
    ) -> None:
        super().__init__(provider, status, error_type)
        self.provider = provider
        self.status = status
        self.error_type = error_type
        self.message = message
        self.request_id = request_id
        self.retryable = retryable

    def __str__(self) -> str:
        return (
            f"{self.provider} call failed: status={self.status} type={self.error_type} "
            f"request_id={self.request_id} retryable={self.retryable}: {self.message}"
        )
