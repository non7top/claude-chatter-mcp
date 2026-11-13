"""The one call made into agy: `agy agentapi send-message`."""
import os
import json
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .endpoint import Endpoint

logger = logging.getLogger("agy_wakeup")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: Optional[str] = None
    endpoint_gone: bool = False


class AgentApi:
    def __init__(self, timeout_seconds: float = 20.0, title: str = "claude-chatter"):
        self._timeout = timeout_seconds
        self._title = title

    async def send_message(self, endpoint: Endpoint, text: str) -> SendResult:
        # The token goes through the child's environment only, never argv or logs.
        env = dict(os.environ)
        env["ANTIGRAVITY_LS_ADDRESS"] = endpoint.ls_address
        env["ANTIGRAVITY_CSRF_TOKEN"] = endpoint.csrf_token
        try:
            proc = await asyncio.create_subprocess_exec(
                endpoint.agy_path, "agentapi", "send-message", f"--title={self._title}",
                endpoint.conversation_id, text,
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except Exception as e:
            return SendResult(False, f"agentapi invocation failed: {e}")
        return self._classify(stdout, proc.returncode, endpoint.conversation_id)

    @staticmethod
    def _classify(stdout: bytes, returncode: Optional[int], conversation_id: str) -> SendResult:
        # agentapi exits 0 even on failure, so the JSON is the only reliable signal.
        try:
            reply = json.loads(stdout.decode("utf-8"))
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            return SendResult(False, f"unparseable agentapi output (exit {returncode})")
        if reply.get("error"):
            error = str(reply["error"])[:300]
            return SendResult(False, error, endpoint_gone="Unavailable" in error)
        sent = (reply.get("response") or {}).get("sendMessage")
        if not isinstance(sent, dict) or sent.get("recipientId") != conversation_id:
            return SendResult(False, "agy did not confirm delivery to the conversation")
        return SendResult(True)
