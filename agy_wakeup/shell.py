"""Client side of `--prime`: runs in agy's shell tool, where the endpoint credentials exist."""
import os
import glob
import json
import shutil
import socket
from typing import Any, Dict, Optional

import psutil

from . import paths
from .control import control_socket_path
from .pinning import AgyProcessPin

ENV_KEYS = ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")


class AgyLocator:
    @staticmethod
    def find_ancestor() -> Optional[psutil.Process]:
        return next((p for p in psutil.Process().parents() if p.name() == "agy"), None)

    @staticmethod
    def resolve_executable(agy: psutil.Process) -> Optional[str]:
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


class ShellPrimer:
    """
    Finds the host process that shares this shell's agy process and hands it
    the endpoint from the environment over its control socket.
    """

    def __init__(self, sessions_dir: Optional[str] = None):
        self._sessions_dir = sessions_dir or paths.SESSIONS_DIR

    def prime(self) -> Dict[str, Any]:
        missing = [k for k in ENV_KEYS if not os.environ.get(k)]
        if missing:
            return {"ok": False, "error": f"missing environment: {', '.join(missing)}; run this from agy's shell tool"}
        agy = AgyLocator.find_ancestor()
        if agy is None:
            return {"ok": False, "error": "no agy process found above this shell"}
        agy_path = AgyLocator.resolve_executable(agy)
        if agy_path is None:
            return {"ok": False, "error": "cannot locate the agy executable"}

        hosts = self._hosts_under(agy)
        if not hosts:
            return {"ok": False, "error": "no bridge found under this agy process"}

        request = json.dumps({
            "op": "prime",
            "conversation_id": os.environ["ANTIGRAVITY_CONVERSATION_ID"],
            "ls_address": os.environ["ANTIGRAVITY_LS_ADDRESS"],
            "csrf_token": os.environ["ANTIGRAVITY_CSRF_TOKEN"],
            "agy_path": agy_path,
            "agy_pid": agy.pid,
            "agy_started": AgyProcessPin.started_at(agy.pid),
        }) + "\n"
        replies = [self._send(ctl_path, request) for ctl_path in hosts.values()]
        return replies[0] if len(replies) == 1 else {"ok": all(r.get("ok") for r in replies), "bridges": replies}

    def _hosts_under(self, agy: psutil.Process) -> Dict[int, str]:
        hosts: Dict[int, str] = {}
        for path in glob.glob(os.path.join(self._sessions_dir, "*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                pid, sock = meta.get("pid"), meta.get("messagingSocketPath")
                if meta.get("kind") != "bg" or not isinstance(pid, int) or not sock:
                    continue
                if agy.pid in {p.pid for p in psutil.Process(pid).parents()}:
                    hosts[pid] = control_socket_path(sock)
            except (OSError, ValueError, psutil.Error):
                continue
        return hosts

    @staticmethod
    def _send(ctl_path: str, request: str) -> Dict[str, Any]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5.0)
                s.connect(ctl_path)
                s.sendall(request.encode("utf-8"))
                return json.loads(s.makefile("r", encoding="utf-8").readline())
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"{ctl_path}: {e}"}
