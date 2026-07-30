"""The dev chat window must never be reachable in production.

Booting the real app needs a database, so this asserts on the two independent
guards instead: the registration condition in main.py and the in-handler check
in dev_chat.py. Both must agree that production is off-limits.
"""

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.config import settings


from app.api.dev_chat_guard import assert_not_production


@pytest.mark.parametrize("env", ["production", "PRODUCTION", "Production"])
def test_handler_guard_rejects_production(monkeypatch, env):
    monkeypatch.setattr(settings, "app_env", env)
    with pytest.raises(HTTPException) as exc:
        assert_not_production()
    # 404 rather than 403: the route should look absent, not merely forbidden.
    assert exc.value.status_code == 404


@pytest.mark.parametrize("env", ["development", "staging", "test", "", None])
def test_handler_guard_allows_non_production(monkeypatch, env):
    monkeypatch.setattr(settings, "app_env", env)
    assert_not_production()


def test_main_registers_dev_chat_only_outside_production():
    """Guard the registration condition itself.

    The handler check above is belt-and-braces; this is the one that actually
    keeps the route out of the production app, so a refactor that drops it
    should fail here.
    """
    main_src = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(main_src.read_text())

    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "dev_chat" in ast.unparse(node)
    ]
    assert guarded, "dev_chat router must be registered inside an if statement"

    for node in guarded:
        condition = ast.unparse(node.test)
        assert "app_env" in condition
        assert "production" in condition
        assert "!=" in condition, "condition must exclude production, not match it"
