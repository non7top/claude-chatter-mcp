"""
MCP stdio entry point built on FastMCP. FastMCP owns the JSON-RPC/stdio
transport, tool schema generation, and standard notifications; this module
wires ClaudeMessagingProtocol into it and holds nothing else.
"""
import os
import sys
import json
import shlex
import signal
import asyncio
import logging
import psutil
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from .protocol import ClaudeMessagingProtocol
from .prime import AgyPoker, control_socket_path

logger = logging.getLogger("SocketBridgeMCP")

INSTRUCTIONS = (
    "Bi-Directional AI Communication MCP Bridge for Claude Code sessions. "
    "Nothing can interrupt you when a message arrives, so check read_messages "
    "at the start of every turn, before and after each piece of work, and "
    "before going idle or ending your turn. Do not poll in a loop to wait for "
    "a reply: after sending, end your turn. A primed Antigravity session is "
    "woken by the reply itself, and an idle Claude Code session may not reply "
    "until someone prompts it. Always call read_messages after a "
    "tools/list_changed notification; it only signals that something "
    "arrived, never carries the message. If you run inside Antigravity, "
    "call prime_push first thing in every conversation and run the shell "
    "command it returns; until you do, nothing can wake you."
)


class _SessionCaptureMiddleware(Middleware):
    """
    Captures the connected ServerSession into the given holder on every
    incoming message - including the very first one (initialize) - so
    inbound socket activity can poke the host even before any tool has ever
    been called. Relying on a tool call to capture the session (the earlier
    approach) left a real gap: a host that connects but doesn't call a tool
    right away would see inbound messages silently dropped with no log trace
    at all, for as long as that gap lasted.
    """
    def __init__(self, holder: Dict[str, Any]):
        self.holder = holder

    async def on_message(self, context, call_next):
        ctx = context.fastmcp_context
        if ctx is not None:
            try:
                self.holder["session"] = ctx.session
            except Exception:
                pass
        return await call_next(context)


def build_mcp_server(protocol: ClaudeMessagingProtocol) -> FastMCP:
    """
    Builds a FastMCP server wired to the given protocol instance. A factory
    rather than a module-level singleton so each process's protocol gets its
    own server with tools closed over it.
    """
    # Holds the most recently seen ServerSession so inbound socket activity
    # (which happens outside any tool call's request context) can still poke
    # the connected host. FastMCP's notification API only supports the
    # standard MCP notification set - there is no way to send our own custom
    # method/params through it, so this uses the standard tools/list_changed
    # signal instead; see INSTRUCTIONS for what the host is expected to do
    # with it.
    last_session_holder: Dict[str, Any] = {"session": None}
    # asyncio.create_task() only holds a WEAK reference to the task it returns -
    # an unreferenced task can be garbage-collected mid-flight, before it
    # actually sends anything, per the asyncio docs' own warning. Keep a strong
    # reference until each notification task finishes.
    background_tasks: set = set()

    def notify_mcp_host(method: str, params: Dict[str, Any]):
        session = last_session_holder["session"]
        if session is None:
            logger.warning("No MCP session captured yet; dropping host notification.")
            return
        try:
            task = asyncio.create_task(session.send_tool_list_changed())
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            logger.info("Dispatched tools/list_changed notification to host.")
        except Exception as e:
            logger.error(f"Failed to send tool_list_changed notification: {e}")

    poker = AgyPoker()
    try:
        under_agy = any(p.name() == "agy" for p in psutil.Process().parents())
    except psutil.Error:
        under_agy = False

    def on_inbound(method: str, params: Dict[str, Any]):
        notify_mcp_host(method, params)
        poker.notify(params.get("sender", "unknown"))

    protocol.on_inbound_message = on_inbound
    control_path = control_socket_path(protocol.bridge_socket_path)

    @asynccontextmanager
    async def _lifespan(server: FastMCP):
        protocol.register_session_descriptor(session_name=protocol.session_name, kind="bg")
        listener_task = asyncio.create_task(protocol.start_bridge_listener())
        control_task = asyncio.create_task(poker.serve_control_socket(control_path))
        try:
            yield
        finally:
            for task in (listener_task, control_task):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if os.path.exists(control_path):
                os.remove(control_path)
            protocol.cleanup_session_descriptor()
            protocol.cleanup_socket()
            logger.info("Bridge stdio handler shut down gracefully.")

    mcp = FastMCP(
        name="claudemessaging",
        version="0.3.0",
        instructions=INSTRUCTIONS,
        lifespan=_lifespan,
        middleware=[_SessionCaptureMiddleware(last_session_holder)]
    )

    @mcp.tool()
    async def list_sessions() -> str:
        """Scans workspace configurations, purges dead session files, and returns active Claude sessions."""
        peers = await protocol.verify_and_purge_sessions()
        return json.dumps(peers, indent=2)

    @mcp.tool()
    async def purge_sessions() -> str:
        """Force scans and cleans up dead socket and session files."""
        peers = await protocol.verify_and_purge_sessions()
        return f"Purge scan completed. Active sessions remaining: {len(peers)}"

    @mcp.tool()
    async def rename_session(new_name: str) -> str:
        """Renames this bridge's announced session descriptor in ~/.claude/sessions/ so Claude Code instances discover it under the new name via ListAgents. Persisted for this workspace, so future restarts keep the renamed identity instead of reverting to the cwd-derived default."""
        old_name = protocol.session_name
        protocol.cleanup_session_descriptor()
        protocol.session_name = new_name
        protocol.register_session_descriptor(session_name=new_name)
        protocol.persist_default_session_name(new_name)
        res = {
            "success": True,
            "previous_name": old_name,
            "new_name": new_name,
            "pid": getattr(protocol, "registered_pid", os.getpid()),
            "descriptor_file": getattr(protocol, "session_json_path", "")
        }
        return json.dumps(res, indent=2)

    @mcp.tool()
    async def send_message(session: str, message: str) -> str:
        """Dispatches an authenticated user message to a target Claude Code session. Fire-and-forget: any real reply the target sends back arrives later as its own inbound activity, observable via read_messages - this does not wait for or return a reply."""
        result = await protocol.send_to_claude(session_identifier=session, message_content=message)
        if not result.get("success", False):
            raise ToolError(result.get("error", "send_message failed"))
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def read_messages(mark_read: bool = True) -> str:
        """Returns {"messages": {...}} with unread inbound messages (oldest first) - the inbox check for this async mailbox. Nothing can reliably interrupt you when a message arrives, so call this after a tools/list_changed notification, or at the start of a turn and before going idle, to see what's new. Marks returned messages as read unless mark_read is set to false (to peek without consuming). Adds a push_hint when this Antigravity session has not primed push yet."""
        result: Dict[str, Any] = {"messages": protocol.get_unread_messages(mark_read=mark_read)}
        if under_agy and not poker.primed:
            result["push_hint"] = (
                "Push is not primed, so nothing can wake you when a message arrives. "
                "Call prime_push once and run the shell command it returns."
            )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def prime_push() -> str:
        """Returns a shell command that, run once in your shell tool, lets this bridge wake you when a message arrives (via agy agentapi). It needs your shell's ANTIGRAVITY_* environment, which MCP servers never receive, so it cannot be run for you. Also reports whether push is already primed."""
        command = f"{shlex.quote(sys.executable)} -m claude_chatter.cli --prime"
        result: Dict[str, Any] = {"primed": poker.primed, "last_error": poker.last_error, "command": command}
        if not poker.primed:
            result["next_step"] = (
                "Run `command` in your shell tool now, exactly as given. Nothing is primed until you do. "
                "It prints JSON with ok=true on success and never prints the token; do not write the "
                "token anywhere. Then end your turn instead of polling."
            )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_responses(msg_id: Optional[str] = None) -> str:
        """Fetches the full inbound and outbound message history cached by the bridge, regardless of read state. Use read_messages instead to check for new inbound messages specifically."""
        resp = protocol.response_store.get(msg_id, {"error": f"Message ID '{msg_id}' not found."}) if msg_id else protocol.response_store
        return json.dumps(resp, indent=2)

    return mcp


def make_protocol(bridge_socket_path: Optional[str], session_name: Optional[str]) -> ClaudeMessagingProtocol:
    return ClaudeMessagingProtocol(bridge_socket_path=bridge_socket_path, session_name=session_name)


async def run_mcp_server(protocol: ClaudeMessagingProtocol):
    """
    SIGTERM has no default Python-level handler - unlike SIGINT, which the
    interpreter itself converts into a catchable KeyboardInterrupt, an
    unhandled SIGTERM kills the process immediately at the OS level, skipping
    all cleanup and exiting 143 (128+SIGTERM) instead of 0. A host that
    gracefully asks this process to stop (e.g. Antigravity's own `/mcp`
    reload, which stops and restarts each configured MCP server) reads that
    143 as this server having failed to stop cleanly, and refuses to reload.

    Cancelling FastMCP's own run_stdio_async() task and waiting for it to
    unwind (so _lifespan's finally block runs the cleanup) was tried first,
    but was observed to hang indefinitely - the task never responds to
    cancellation and the process never exits at all. Rather than depend on
    FastMCP's internal shutdown behavior, the handler does the cleanup that
    actually matters (the session descriptor and socket file - the same two
    calls _lifespan's finally block makes) directly and exits immediately.
    """
    mcp = build_mcp_server(protocol)
    loop = asyncio.get_running_loop()

    def _handle_signal(signum):
        logger.info(f"Received signal {signum}; cleaning up and exiting.")
        protocol.cleanup_session_descriptor()
        protocol.cleanup_socket()
        try:
            os.remove(control_socket_path(protocol.bridge_socket_path))
        except OSError:
            pass
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            pass

    await mcp.run_stdio_async(show_banner=False)
