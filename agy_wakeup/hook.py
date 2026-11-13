"""
An Antigravity PreInvocation hook that makes sure the conversation is primed
for push. Pure stdlib and cheap, since it runs before every model invocation.

A hook process does not get the language-server token, so it cannot prime by
itself. Instead it injects an ephemeralMessage telling the agent to run the
prime command in its own shell, where the token exists.
"""
import os
import json
import time
from typing import Any, Dict, Optional

from . import paths

# A failed priming must not be retried before every model invocation.
RETRY_AFTER_SECONDS = 600
DEFAULT_NOTICE = (
    "[agy-wakeup] Push is not enabled for this conversation, so messages from other "
    "sessions cannot wake you. Before anything else, run `agy-wakeup --prime` in your "
    "shell, exactly as given. Then continue with the user's request."
)


class PrimeHook:
    def __init__(self, notice: str = DEFAULT_NOTICE, primed_dir: Optional[str] = None):
        self._notice = notice
        self._primed_dir = primed_dir

    def run(self, stdin_text: str) -> str:
        try:
            hook_input = json.loads(stdin_text) if stdin_text.strip() else {}
        except ValueError:
            hook_input = {}
        return json.dumps(self.decide(hook_input))

    def decide(self, hook_input: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        primed_dir = self._primed_dir or paths.PRIMED_DIR
        conv = hook_input.get("conversationId")
        if not isinstance(conv, str) or not paths.CONVERSATION_ID.match(conv):
            return {}

        try:
            with open(os.path.join(primed_dir, f"{conv}.json"), "r", encoding="utf-8") as f:
                if self._pid_alive(json.load(f).get("bridge_pid")):
                    return {}
        except (OSError, ValueError):
            pass

        now = time.time() if now is None else now
        attempt_path = os.path.join(primed_dir, f"{conv}.attempt")
        try:
            if now - os.path.getmtime(attempt_path) < RETRY_AFTER_SECONDS:
                return {}
        except OSError:
            pass
        try:
            os.makedirs(primed_dir, exist_ok=True)
            with open(attempt_path, "w", encoding="utf-8"):
                pass
        except OSError:
            return {}

        return {"injectSteps": [{"ephemeralMessage": self._notice}]}

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
