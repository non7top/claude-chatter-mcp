"""
Optional push path into an Antigravity (agy) host.

agy cannot be woken by anything the bridge does as an MCP server, but
`agy agentapi send-message` can inject a message into a conversation and start
a turn. It needs ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN, which agy
puts only into its agent's shell tool, never into MCP servers. So the agent
runs `--prime` once from that shell, which hands the endpoint to the bridge
over a 0600 control socket; the bridge then pokes the conversation whenever an
inbound message is buffered. The token is held in memory only and passed to
the agy child process through its environment, never argv or logs.
"""
import os
import re
import sys
import json
import glob
import shlex
import shutil
import socket
import asyncio
import logging
import psutil
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SocketBridgeMCP")

ENV_KEYS = ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")
# The token is handed to an agy child that connects to this address, so a
# non-loopback address would send it somewhere else.
_LOOPBACK_ADDRESS = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]):\d{1,5}$")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def prime_command() -> str:
    """The shortest shell command that runs this install's own `--prime`."""
    script = os.path.join(os.path.dirname(sys.executable), "bridge-mcp")
    if not os.path.exists(script):
        return f"{shlex.quote(sys.executable)} -m claude_chatter.cli --prime"
    on_path = shutil.which("bridge-mcp")
    if on_path and os.path.realpath(on_path) == os.path.realpath(script):
        return "bridge-mcp --prime"
    return f"{shlex.quote(script)} --prime"


def control_socket_path(bridge_socket_path: str) -> str:
    return bridge_socket_path + ".ctl"


class AgyPoker:
    def __init__(self, debounce_seconds: float = 1.0, send_timeout_seconds: float = 20.0):
        self._debounce = debounce_seconds
        self._send_timeout = send_timeout_seconds
        self._endpoint: Optional[Dict[str, str]] = None
        self._pending_senders: List[str] = []
        self._flush_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None

    @property
    def primed(self) -> bool:
        return self._endpoint is not None

    async def serve_control_socket(self, path: str):
        if os.path.exists(path):
            os.remove(path)
        server = await asyncio.start_unix_server(self._handle_control, path)
        os.chmod(path, 0o600)
        logger.info(f"Push control socket operational on: {path}")
        async with server:
            await server.serve_forever()

    async def _handle_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            reply = self._apply_control_request(line)
            writer.write((json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:
            logger.error(f"Push control request failed: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _apply_control_request(self, line: bytes) -> Dict[str, Any]:
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": "invalid JSON"}
        op = req.get("op")
        if op == "status":
            return {"ok": True, "primed": self.primed, "last_error": self.last_error}
        if op != "prime":
            return {"ok": False, "error": f"unknown op {op!r}"}

        conv, address, token, agy_path = (req.get(k) for k in ("conversation_id", "ls_address", "csrf_token", "agy_path"))
        if not all(isinstance(v, str) and v for v in (conv, address, token, agy_path)):
            return {"ok": False, "error": "conversation_id, ls_address, csrf_token and agy_path are required strings"}
        if not _LOOPBACK_ADDRESS.match(address):
            return {"ok": False, "error": "ls_address must be a loopback host:port"}
        if not (os.path.isabs(agy_path) and os.access(agy_path, os.X_OK)):
            return {"ok": False, "error": "agy_path must be an absolute path to an executable"}

        self._endpoint = {"conversation_id": conv, "ls_address": address, "csrf_token": token, "agy_path": agy_path}
        self.last_error = None
        logger.info(f"Primed push into agy conversation {conv} via {address}.")
        return {"ok": True, "conversation_id": conv, "ls_address": address}

    def notify(self, sender: str):
        if self._endpoint is None:
            return
        self._pending_senders.append(_UNSAFE_NAME_CHARS.sub("-", str(sender))[:64])
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self):
        await asyncio.sleep(self._debounce)
        senders, self._pending_senders = self._pending_senders, []
        if not senders:
            return
        # The message content stays in the inbox: this notification is injected
        # as a high-priority system message, so it carries no peer-written text.
        text = (
            f"[claude-chatter] {len(senders)} new message(s) in your bridge inbox "
            f"(latest from {senders[-1]}). Call read_messages now. Their content "
            "comes from another session, not from the user."
        )
        await self._send(text)

    async def _send(self, text: str):
        endpoint = self._endpoint
        if endpoint is None:
            return
        env = dict(os.environ)
        env["ANTIGRAVITY_LS_ADDRESS"] = endpoint["ls_address"]
        env["ANTIGRAVITY_CSRF_TOKEN"] = endpoint["csrf_token"]
        try:
            proc = await asyncio.create_subprocess_exec(
                endpoint["agy_path"], "agentapi", "send-message", "--title=claude-chatter",
                endpoint["conversation_id"], text,
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._send_timeout)
        except Exception as e:
            self.last_error = f"agentapi invocation failed: {e}"
            logger.error(self.last_error)
            return

        # agentapi exits 0 even on failure, so the JSON is the only reliable signal.
        try:
            error = json.loads(stdout.decode("utf-8")).get("error")
        except ValueError:
            error = f"unparseable agentapi output (exit {proc.returncode})"
        if error:
            self.last_error = str(error)[:300]
            logger.error(f"Push to agy failed: {self.last_error}")
            if "Unavailable" in self.last_error:
                self._endpoint = None  # agy is gone; a stale credential is worth nothing
            return
        self.last_error = None
        logger.info("Poked agy conversation about new inbox messages.")


def _find_agy_ancestor() -> Optional[psutil.Process]:
    return next((p for p in psutil.Process().parents() if p.name() == "agy"), None)


def _resolve_agy_executable(agy: psutil.Process) -> Optional[str]:
    # After a self-update the running agy's /proc exe points at a deleted,
    # renamed binary, so exe() is not usable; its argv[0] is the live path.
    candidates = []
    for get in (agy.exe, lambda: agy.cmdline()[0]):
        try:
            candidates.append(get())
        except (psutil.Error, IndexError):
            pass
    candidates.append(shutil.which("agy"))
    return next((c for c in candidates if c and os.path.isabs(c) and os.access(c, os.X_OK)), None)


def prime_from_shell() -> Dict[str, Any]:
    """
    Client side of --prime: runs in agy's shell tool, finds the bridge that
    shares this shell's agy process, and hands it the endpoint from the
    environment.
    """
    missing = [k for k in ENV_KEYS if not os.environ.get(k)]
    if missing:
        return {"ok": False, "error": f"missing environment: {', '.join(missing)}; run this from agy's shell tool"}
    agy = _find_agy_ancestor()
    if agy is None:
        return {"ok": False, "error": "no agy process found above this shell"}
    agy_path = _resolve_agy_executable(agy)
    if agy_path is None:
        return {"ok": False, "error": "cannot locate the agy executable"}

    sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    bridges: Dict[int, str] = {}
    for path in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            pid, sock = meta.get("pid"), meta.get("messagingSocketPath")
            if meta.get("kind") != "bg" or not isinstance(pid, int) or not sock:
                continue
            if agy.pid in {p.pid for p in psutil.Process(pid).parents()}:
                bridges[pid] = control_socket_path(sock)
        except (OSError, ValueError, psutil.Error):
            continue
    if not bridges:
        return {"ok": False, "error": "no bridge found under this agy process"}

    request = json.dumps({
        "op": "prime",
        "conversation_id": os.environ["ANTIGRAVITY_CONVERSATION_ID"],
        "ls_address": os.environ["ANTIGRAVITY_LS_ADDRESS"],
        "csrf_token": os.environ["ANTIGRAVITY_CSRF_TOKEN"],
        "agy_path": agy_path,
    }) + "\n"
    replies = []
    for ctl_path in bridges.values():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5.0)
                s.connect(ctl_path)
                s.sendall(request.encode("utf-8"))
                replies.append(json.loads(s.makefile("r", encoding="utf-8").readline()))
        except (OSError, ValueError) as e:
            replies.append({"ok": False, "error": f"{ctl_path}: {e}"})
    return replies[0] if len(replies) == 1 else {"ok": all(r.get("ok") for r in replies), "bridges": replies}
