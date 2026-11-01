"""`jhunt-api` entry point: serves the digest page (ADR 0012).

It lives here in the composition root because the page queues Capture runs,
which needs the registry. It listens on 127.0.0.1 only: there is no login
yet, so the page must not be reachable from another machine. JHUNT_API_PORT
picks the port.
"""

import uvicorn
from sqlalchemy import create_engine

from jhunt.api.digest import create_app
from jhunt.app.registry import build_registry
from jhunt.platform.config import get_settings

HOST = "127.0.0.1"


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url(), pool_pre_ping=True)
    app = create_app(engine, build_registry(settings, engine), port=settings.jhunt_api_port)
    uvicorn.run(app, host=HOST, port=settings.jhunt_api_port)
