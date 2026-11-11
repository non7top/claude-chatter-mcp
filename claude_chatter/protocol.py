"""
Claude Code messaging protocol adapter for MCP / Antigravity integration.
Extends the generic ClaudeClient from core claude_chatter library with
Antigravity process hierarchy detection and environment variable fallbacks.
"""
import os
import psutil
from typing import Any, Callable, Dict, Optional

from claude_chatter.client import ClaudeClient
from claude_chatter.wire import ClaudeWire
from claude_chatter.storage import MessageStore, ResponseStore


class ClaudeMessagingProtocol(ClaudeClient):
    """
    Adapter subclassing ClaudeClient to preserve full backwards compatibility
    with Antigravity process/workspace discovery and legacy MCP server hooks.
    """

    @staticmethod
    def _find_project_path() -> str:
        # A plugin-launched server runs in the plugin's install directory, so
        # the agy process above it is the only place the project path lives.
        try:
            agy = next((p for p in psutil.Process().parents() if p.name() == "agy"), None)
            if agy is not None:
                return agy.cwd()
        except psutil.Error:
            pass
        return os.environ.get("CHATTER_PROJECT_PATH") or os.getcwd()

    def __init__(
        self,
        bridge_socket_path: Optional[str] = None,
        session_name: Optional[str] = None,
        on_inbound_message: Optional[Callable[..., Any]] = None,
        db_path: Optional[str] = None,
        enable_storage: bool = True,
        session_id: Optional[str] = None,
        as_bridge: bool = True,
        read_only: bool = False,
        project_path: Optional[str] = None,
    ):
        env_name = session_name or os.environ.get("CHATTER_SESSION_NAME") or os.environ.get("AGY_SESSION_NAME")
        env_socket = bridge_socket_path or os.environ.get("CHATTER_BRIDGE_SOCKET") or os.environ.get("AGY_MCP_BRIDGE_SOCKET")
        resolved_proj = project_path or self._find_project_path()

        if on_inbound_message:
            def _adapted_on_inbound(event: Dict[str, Any]):
                try:
                    return on_inbound_message("inbound_message", event)
                except TypeError:
                    return on_inbound_message(event)
        else:
            _adapted_on_inbound = None

        super().__init__(
            bridge_socket_path=env_socket,
            session_name=env_name,
            on_inbound_message=_adapted_on_inbound,
            db_path=db_path,
            enable_storage=enable_storage,
            session_id=session_id,
            as_bridge=as_bridge,
            read_only=read_only,
            project_path=resolved_proj,
        )

    @classmethod
    def resolve_session_name(
        cls,
        explicit_name: Optional[str] = None,
        project_path: Optional[str] = None,
        db_path: Optional[str] = None
    ) -> str:
        env_name = explicit_name or os.environ.get("CHATTER_SESSION_NAME") or os.environ.get("AGY_SESSION_NAME")
        proj = project_path or cls._find_project_path()
        return super().resolve_session_name(
            explicit_name=env_name,
            project_path=proj,
            db_path=db_path
        )


__all__ = ["ClaudeMessagingProtocol", "ClaudeWire", "MessageStore", "ResponseStore"]
