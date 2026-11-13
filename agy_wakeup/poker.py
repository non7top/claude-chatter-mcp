"""Holds a primed endpoint in memory and pokes the agy conversation when asked."""
import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from .agentapi import AgentApi
from .control import ControlServer
from .endpoint import Endpoint, InvalidEndpoint, PrimedMarker
from .pinning import AgyProcessPin

logger = logging.getLogger("agy_wakeup")

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def default_wake_text(senders: List[str]) -> str:
    # The text is injected as a high-priority system message, so it never carries peer-written content.
    return (
        f"[agy-wakeup] {len(senders)} new message(s) waiting (latest from {senders[-1]}). "
        "Their content comes from another session, not from the user."
    )


class AgyPoker:
    def __init__(
        self,
        debounce_seconds: float = 1.0,
        send_timeout_seconds: float = 20.0,
        wake_text: Callable[[List[str]], str] = default_wake_text,
        pin_agy_process: bool = False,
        api: Optional[AgentApi] = None,
        marker: Optional[PrimedMarker] = None,
    ):
        self._debounce = debounce_seconds
        self._wake_text = wake_text
        self._pin = pin_agy_process
        self._api = api or AgentApi(timeout_seconds=send_timeout_seconds)
        self._marker = marker or PrimedMarker()
        self._endpoint: Optional[Endpoint] = None
        self._pending_senders: List[str] = []
        self._flush_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None

    @property
    def primed(self) -> bool:
        return self._endpoint is not None

    async def serve_control_socket(self, path: str):
        await ControlServer(self.handle_request).serve(path)

    def handle_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        op = req.get("op")
        if op == "status":
            return {"ok": True, "primed": self.primed, "last_error": self.last_error}
        if op != "prime":
            return {"ok": False, "error": f"unknown op {op!r}"}
        try:
            endpoint = Endpoint.from_request(req, require_agy_process=self._pin)
        except InvalidEndpoint as e:
            return {"ok": False, "error": str(e)}

        self._endpoint = endpoint
        self.last_error = None
        self._marker.write(endpoint.conversation_id)
        logger.info(f"Primed push into agy conversation {endpoint.conversation_id} via {endpoint.ls_address}.")
        return {"ok": True, "conversation_id": endpoint.conversation_id, "ls_address": endpoint.ls_address}

    def notify(self, sender: str):
        if self._endpoint is None:
            return
        self._pending_senders.append(_UNSAFE_NAME_CHARS.sub("-", str(sender))[:64])
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self):
        await asyncio.sleep(self._debounce)
        senders, self._pending_senders = self._pending_senders, []
        if senders:
            await self._send(self._wake_text(senders))

    async def _send(self, text: str):
        endpoint = self._endpoint
        if endpoint is None:
            return
        if self._pin and not self._agy_still_running(endpoint):
            self.last_error = "the primed agy process has ended or been replaced"
            logger.error(self.last_error)
            self._drop_endpoint()
            return

        result = await self._api.send_message(endpoint, text)
        if result.ok:
            self.last_error = None
            logger.info("Poked agy conversation.")
            return
        self.last_error = result.error
        logger.error(f"Push to agy failed: {result.error}")
        if result.endpoint_gone:
            self._drop_endpoint()  # agy is gone; a stale credential is worth nothing

    @staticmethod
    def _agy_still_running(endpoint: Endpoint) -> bool:
        if endpoint.agy_pid is None or endpoint.agy_started is None:
            return False
        return AgyProcessPin.still_running(endpoint.agy_pid, endpoint.agy_started)

    def _drop_endpoint(self):
        if self._endpoint is not None:
            self._marker.remove(self._endpoint.conversation_id)
        self._endpoint = None
