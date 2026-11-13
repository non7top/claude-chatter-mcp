"""The agy conversation to wake: its endpoint credentials and the marker that records it is primed."""
import os
import re
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import paths

logger = logging.getLogger("agy_wakeup")

# The token is handed to an agy child that connects to this address, so a
# non-loopback address would send it somewhere else.
_LOOPBACK_ADDRESS = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]):\d{1,5}$")


class InvalidEndpoint(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    conversation_id: str
    ls_address: str
    csrf_token: str
    agy_path: str
    agy_pid: Optional[int] = None
    agy_started: Optional[float] = None

    @classmethod
    def from_request(cls, req: Dict[str, Any], require_agy_process: bool = False) -> "Endpoint":
        conv, address, token, agy_path = (req.get(k) for k in ("conversation_id", "ls_address", "csrf_token", "agy_path"))
        if not all(isinstance(v, str) and v for v in (conv, address, token, agy_path)):
            raise InvalidEndpoint("conversation_id, ls_address, csrf_token and agy_path are required strings")
        if not paths.CONVERSATION_ID.match(conv):
            raise InvalidEndpoint("conversation_id has an unexpected format")
        if not _LOOPBACK_ADDRESS.match(address):
            raise InvalidEndpoint("ls_address must be a loopback host:port")
        if not (os.path.isabs(agy_path) and os.access(agy_path, os.X_OK)):
            raise InvalidEndpoint("agy_path must be an absolute path to an executable")

        pid, started = req.get("agy_pid"), req.get("agy_started")
        has_process = (
            isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and isinstance(started, (int, float)) and not isinstance(started, bool)
        )
        if require_agy_process and not has_process:
            raise InvalidEndpoint("agy_pid and agy_started are required when agy process pinning is on")
        return cls(conv, address, token, agy_path, pid if has_process else None, float(started) if has_process else None)


class PrimedMarker:
    """Lets the prime hook cheaply tell "already primed" without ever holding the token."""

    @staticmethod
    def path(conversation_id: str) -> str:
        return os.path.join(paths.PRIMED_DIR, f"{conversation_id}.json")

    def write(self, conversation_id: str):
        try:
            os.makedirs(paths.PRIMED_DIR, exist_ok=True)
            with open(self.path(conversation_id), "w", encoding="utf-8") as f:
                json.dump({"bridge_pid": os.getpid(), "conversation_id": conversation_id}, f)
        except OSError as e:
            logger.error(f"Failed to write primed marker: {e}")

    def remove(self, conversation_id: str):
        try:
            os.remove(self.path(conversation_id))
        except OSError:
            pass
