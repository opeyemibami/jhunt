"""A local web server whose pages a test can change (Capture, capture-once)."""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@dataclass
class Site:
    """Pages served at http://127.0.0.1:<port>/<path>: path -> (status, body)."""

    base: str = ""
    pages: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    hits: list[str] = field(default_factory=list)

    def url(self, path: str) -> str:
        return f"{self.base}/{path}"


@pytest.fixture
def site() -> Iterator[Site]:
    served = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the name http.server calls
            path = self.path.lstrip("/")
            served.hits.append(path)
            status, body = served.pages.get(path, (404, b"not found"))
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass  # keep test output quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    served.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield served
    server.shutdown()
    server.server_close()
