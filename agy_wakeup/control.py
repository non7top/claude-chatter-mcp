"""The 0600 Unix control socket a primed host listens on for prime and status requests."""
import os
import json
import asyncio
import logging
from typing import Any, Callable, Dict

logger = logging.getLogger("agy_wakeup")


def control_socket_path(bridge_socket_path: str) -> str:
    return bridge_socket_path + ".ctl"


class ControlServer:
    """One JSON request line in, one JSON reply line out; what the request means is the handler's business."""

    def __init__(self, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        self._handler = handler

    async def serve(self, path: str):
        if os.path.exists(path):
            os.remove(path)
        server = await asyncio.start_unix_server(self._handle, path)
        os.chmod(path, 0o600)
        logger.info(f"Push control socket operational on: {path}")
        async with server:
            await server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            try:
                request = json.loads(line.decode("utf-8"))
            except ValueError:
                reply = {"ok": False, "error": "invalid JSON"}
            else:
                reply = self._handler(request) if isinstance(request, dict) else {"ok": False, "error": "invalid JSON"}
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
