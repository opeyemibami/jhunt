"""The only code in the model gateway that does network I/O.

Adapters keep request building, response parsing and error mapping as pure
functions; this module sends the request. Moving to async httpx changes this
module and the adapters' thin `call` functions, nothing else.
"""

from dataclasses import dataclass

import httpx

from jhunt.platform.models.types import ModelCallError

# Generous read timeout: a long completion can take a while without streaming.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: httpx.Headers
    body: object  # parsed JSON, or None when the body was not JSON


def post_json(
    client: httpx.Client,
    provider: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, object],
) -> HttpResult:
    try:
        response = client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise ModelCallError(provider, None, "timeout", str(exc), None, retryable=True) from exc
    except httpx.TransportError as exc:
        raise ModelCallError(provider, None, "connection", str(exc), None, retryable=True) from exc
    try:
        parsed: object = response.json()
    except ValueError:
        parsed = None
    return HttpResult(response.status_code, response.headers, parsed)


def redact(error: ModelCallError, secret: str) -> ModelCallError:
    """Belt and braces: drop the key from a provider message if it was echoed back."""
    error.message = error.message.replace(secret, "[redacted]")
    return error
