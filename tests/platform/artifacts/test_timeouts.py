"""A stalled storage call fails within its timeout, never hangs (ADR 0002).

The server here accepts connections and never answers: what a stalled network
looks like to the client. It only stands in for the network, to make the
stall; every other storage test uses the real CI bucket. The timeouts are
shortened through the module's own constants, so the test takes seconds.
"""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage
from google.oauth2.credentials import Credentials

from jhunt.platform.artifacts import RecordStore, StagingStore, records

# Short stand-ins for TIMEOUT and the retry deadline, so a stall costs seconds.
CONNECT_READ = (0.5, 0.5)
DEADLINE = 1.5
# Well under the real read timeout (30 s): fails only if the timeouts aren't used.
LIMIT = 8.0


@pytest.fixture
def silent_server() -> Iterator[str]:
    """http://127.0.0.1:<port> that accepts every connection and never replies."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                held.append(conn)  # kept open, never answered
            except TimeoutError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join()
    for conn in held:
        conn.close()
    server.close()


@pytest.fixture
def short_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(records, "TIMEOUT", CONNECT_READ)
    monkeypatch.setattr(records, "RETRY", records.RETRY.with_timeout(DEADLINE))


def client_for(endpoint: str, credentials: Any) -> storage.Client:
    return storage.Client(
        project="test",
        credentials=credentials,
        client_options={"api_endpoint": endpoint},
    )


def fails_quickly(call: Callable[[], object]) -> float:
    start = time.monotonic()
    with pytest.raises(Exception) as raised:  # any error, as long as it's quick
        call()
    elapsed = time.monotonic() - start
    assert elapsed < LIMIT, f"took {elapsed:.1f}s: {raised.value!r}"
    return elapsed


@pytest.mark.usefixtures("short_timeouts")
@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.existing("k"),  # get_blob
        lambda s: s.get("k", 1, "0" * 64),  # download_as_bytes
        lambda s: s.put("k", b"x", "text/plain"),  # upload_from_string
        lambda s: s.put_or_adopt("k", b"x", "text/plain"),
    ],
    ids=["existing", "get", "put", "put_or_adopt"],
)
def test_a_stalled_call_fails_within_the_timeout(
    silent_server: str, call: Callable[[Any], object]
) -> None:
    for store_type in (RecordStore, StagingStore):
        store = store_type("bucket", client_for(silent_server, AnonymousCredentials()))
        fails_quickly(lambda: call(store))  # noqa: B023  # called right here


@pytest.mark.usefixtures("short_timeouts")
def test_a_stalled_credential_refresh_fails_within_the_timeout(silent_server: str) -> None:
    # No token yet: the first call refreshes it first, from a token endpoint
    # that never answers. The refresh is bounded by the call's timeout too.
    credentials = Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token="refresh",
        token_uri=f"{silent_server}/token",
        client_id="id",
        client_secret="secret",
    )
    store = RecordStore("bucket", client_for(silent_server, credentials))
    fails_quickly(lambda: store.existing("k"))


def test_the_real_timeouts() -> None:
    # Connect 10 s, read 30 s; retries end after 60 s in all.
    assert records.TIMEOUT == (10.0, 30.0)
    assert records.RETRY.timeout == 60.0
