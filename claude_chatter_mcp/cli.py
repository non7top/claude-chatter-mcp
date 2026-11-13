"""
CLI entry point. Handles the MCP server (default mode) and the Antigravity
prime modes; the plain claude-chatter modes (--list, --send, ...) are handed
to the claude-chatter library's own CLI.
"""
import os
import sys
import json
import asyncio
import logging
import argparse
from logging.handlers import RotatingFileHandler

# mcp_server pulls in fastmcp, which only the default (MCP stdio) mode needs -
# import it lazily so the library modes don't pay that cost.

_LOG_FORMAT = "%(asctime)s [%(levelname)s] [pid=%(process)d] %(message)s"
_log_handlers = [logging.StreamHandler(sys.stderr)]
try:
    # A host-spawned bridge (uvx via Antigravity) has no persistent stderr to
    # read after the fact - a real log file is the difference between "catch
    # it live with strace" and "just read what happened." Shared across all
    # bridge processes/workspaces; pid in the format string disambiguates
    # interleaved lines. Rotated to bound growth on a long-lived daemon.
    _log_dir = os.path.expanduser("~/.claude/logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_handlers.append(
        RotatingFileHandler(
            os.path.join(_log_dir, "claude-chatter.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3
        )
    )
except OSError:
    pass  # fall back to stderr-only rather than fail the whole process over logging

logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=_log_handlers)
logger = logging.getLogger("SocketBridgeMCP")

# The library only knows the CHATTER_* names; the AGY_* ones are this
# package's older spellings and are still honored.
_AGY_ENV_ALIASES = {
    "AGY_SESSION_NAME": "CHATTER_SESSION_NAME",
    "AGY_MCP_BRIDGE_SOCKET": "CHATTER_BRIDGE_SOCKET",
}

_LIBRARY_MODES = ("standalone", "list", "jobs", "inbox", "history", "send", "purge")


def _apply_env_aliases():
    for old, new in _AGY_ENV_ALIASES.items():
        if old in os.environ and new not in os.environ:
            os.environ[new] = os.environ[old]


def main():
    parser = argparse.ArgumentParser(description="Claude Bridge MCP Server & Standalone CLI Tool")
    parser.add_argument("--socket", type=str, default=None, help="Path to bridge Unix socket")
    parser.add_argument("--name", type=str, default=None, help="Session name announced to Claude Code (default: derived from cwd, stable across restarts)")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database for message persistence")
    parser.add_argument("--project-path", type=str, default=None, help="Explicit project directory path")

    mode_group = parser.add_argument_group("Execution Modes")
    mode_group.add_argument("--standalone", "--daemon", action="store_true", help="Run as standalone daemon server without stdio MCP loop")
    mode_group.add_argument("--list", action="store_true", help="List all active Claude Code sessions")
    mode_group.add_argument("--jobs", action="store_true", help="List Claude Code background jobs (including finished ones) from ~/.claude/jobs")
    mode_group.add_argument("--inbox", action="store_true", help="List unread inbound messages from persisted inbox")
    mode_group.add_argument("--history", action="store_true", help="List full persisted message history")
    mode_group.add_argument("--send", nargs=2, metavar=("SESSION", "MESSAGE"), help="Send message to target Claude Code session")
    mode_group.add_argument("--prime", action="store_true", help="From an Antigravity shell tool: hand this agy's push endpoint to its bridge so it can wake the session on inbound messages")
    mode_group.add_argument("--prime-hook", action="store_true", help="Antigravity PreInvocation hook: reads hook JSON on stdin and prints the JSON that makes the session prime push (see --prime)")
    mode_group.add_argument("--purge", action="store_true", help="Proactively scan and purge dead session files")

    args = parser.parse_args()

    if args.prime_hook or args.prime:
        from .agy_glue import make_prime
        prime = make_prime()
        if args.prime_hook:
            print(prime.run_hook(sys.stdin.read()))
            sys.exit(0)
        result = prime.prime_from_shell()
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    _apply_env_aliases()

    if any(getattr(args, mode) for mode in _LIBRARY_MODES):
        from claude_chatter.cli import main as library_main
        library_main()
        return

    from .mcp_server import run_mcp_server, make_protocol
    protocol = make_protocol(args.socket, args.name, db_path=args.db, project_path=args.project_path)
    try:
        asyncio.run(run_mcp_server(protocol))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down bridge daemon gracefully.")
    except Exception as e:
        logger.error(f"Fatal server error: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
