"""Production guard for the dev chat window.

Split out from dev_chat.py so it can be imported (and tested) without pulling in
the agent, which builds a DB engine at import time.
"""

from fastapi import HTTPException

from app.core.config import settings


def assert_not_production() -> None:
    """Raise 404 in production.

    Belt-and-braces: main.py already skips registering the router outside
    non-production environments. 404 rather than 403 so the route looks absent
    rather than merely forbidden.
    """
    if (settings.app_env or "development").lower() == "production":
        raise HTTPException(status_code=404, detail="Not found")
