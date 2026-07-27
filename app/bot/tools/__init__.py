"""Wishkr tools package.

Structure:
- schemas.py:  Hand-written Gemini FunctionDeclarations (single source of truth).
- functions.py: Pure async implementations (db, phone, args) -> dict.
- adapter.py:  SchemaFunctionTool binding declarations to Python wrappers + ADK bridge.
- registry.py: Back-compat shim around schemas.TOOL_DECLARATIONS.
"""

from app.bot.tools.adapter import (
    ALL_TOOLS,
    TOOL_BY_NAME,
    SchemaFunctionTool,
    set_context,
    reset_context,
    execute_tool,
)
from app.bot.tools.schemas import TOOL_DECLARATIONS, TOOL_SPECS, get_declaration, get_tool_spec

__all__ = [
    "ALL_TOOLS",
    "TOOL_BY_NAME",
    "SchemaFunctionTool",
    "set_context",
    "reset_context",
    "execute_tool",
    "TOOL_DECLARATIONS",
    "TOOL_SPECS",
    "get_declaration",
    "get_tool_spec",
]
