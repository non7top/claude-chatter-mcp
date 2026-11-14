"""
Idle notices for sessions that subscribed with notify_when_idle: the agy Stop hook
(--stop-hook) tells its bridge over the .ctl socket, and the bridge hands the
subscribers' notices to the library. Kept free of fastmcp imports: the hook runs
at the end of every agy turn and must stay cheap.
"""
import re
import json
import time
import glob
import socket
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Set

import psutil
from antigravity_wakeup import paths
from antigravity_wakeup.control import control_socket_path
from antigravity_wakeup.shell import AgyLocator

logger = logging.getLogger("SocketBridgeMCP")

CONTROL_TIMEOUT_SECONDS = 2.0
_DETAIL_MAX = 200
_UNSAFE = re.compile(r"[^A-Za-z0-9 _.,:()-]")


def bridge_control_paths(agy: psutil.Process, sessions_dir: Optional[str] = None) -> List[str]:
    """Control sockets of the bridges (kind bg descriptors) running under the given agy process."""
    found: List[str] = []
    for path in glob.glob(f"{sessions_dir or paths.SESSIONS_DIR}/*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            pid, sock = meta.get("pid"), meta.get("messagingSocketPath")
            if meta.get("kind") != "bg" or not isinstance(pid, int) or not sock:
                continue
            if agy.pid in {p.pid for p in psutil.Process(pid).parents()}:
                found.append(control_socket_path(sock))
        except (OSError, ValueError, psutil.Error):
            continue
    return found


def _request(ctl_path: str, request: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CONTROL_TIMEOUT_SECONDS)
            s.connect(ctl_path)
            s.sendall((json.dumps(request) + "\n").encode("utf-8"))
            return json.loads(s.makefile("r", encoding="utf-8").readline())
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"{ctl_path}: {e}"}


def run_stop_hook(stdin_text: str, sessions_dir: Optional[str] = None, now: Optional[float] = None) -> str:
    """
    Returns the hook's stdout, always "{}" so the agent stops as usual. Does nothing
    unless the turn ended with no background work left (fullyIdle), and never raises:
    a Stop hook runs synchronously and must not hold the agent up.
    """
    try:
        hook_input = json.loads(stdin_text) if stdin_text.strip() else {}
        if isinstance(hook_input, dict) and hook_input.get("fullyIdle") is True:
            agy = AgyLocator.find_ancestor()
            if agy is not None:
                reason = _UNSAFE.sub("", str(hook_input.get("terminationReason") or ""))[:64]
                request = {
                    "op": "idle",
                    "finished_at_ms": int((time.time() if now is None else now) * 1000),
                    "detail": f"turn ended ({reason})" if reason else "turn ended",
                }
                for ctl_path in bridge_control_paths(agy, sessions_dir):
                    _request(ctl_path, request)
    except Exception as e:
        logger.error(f"Stop hook failed: {e}")
    return "{}"


def make_control_handler(protocol: Any, poker: Any) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """
    The bridge's control-socket handler: the idle op goes to the library, everything
    else (prime, status) to the poker. Must be called with the event loop running.
    """
    tasks: Set[asyncio.Task] = set()

    def finished(task: asyncio.Task):
        tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error(f"Idle notice task failed: {task.exception()}")
        elif not task.cancelled():
            logger.info(f"Idle notices sent: {task.result()}")

    def handle(req: Dict[str, Any]) -> Dict[str, Any]:
        if req.get("op") != "idle":
            return poker.handle_request(req)
        pending = getattr(protocol, "pending_idle_subscriptions", None)
        publish = getattr(protocol, "publish_idle", None)
        if pending is None or publish is None:
            return {"ok": False, "error": "the installed claude-chatter cannot publish idle notices"}
        count = pending()
        if count:
            finished_at = req.get("finished_at_ms")
            detail = req.get("detail")
            task = asyncio.ensure_future(publish(
                finished_at_ms=finished_at if isinstance(finished_at, int) and not isinstance(finished_at, bool) else None,
                detail=_UNSAFE.sub("", detail)[:_DETAIL_MAX] if isinstance(detail, str) else None,
            ))
            tasks.add(task)
            task.add_done_callback(finished)
        return {"ok": True, "pending": count}

    return handle
