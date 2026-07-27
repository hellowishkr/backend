"""Thin compatibility shim.

Historically this file held ``Tool`` / ``ToolParam`` dataclasses that were
never actually sent to Gemini (ADK derived schemas from function signatures
instead). The authoritative schemas now live in ``schemas.py`` as
``types.FunctionDeclaration`` objects.

This module is retained only to keep ``get_all_tool_names()`` available for
any legacy import; new code should import from ``schemas`` directly.
"""

from app.bot.tools.schemas import TOOL_DECLARATIONS


def get_all_tool_names() -> list[str]:
    """Return the names of every declared tool, in registration order."""
    return list(TOOL_DECLARATIONS.keys())


# Back-compat alias.
TOOLS = TOOL_DECLARATIONS
