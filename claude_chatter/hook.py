"""
`--prime-hook`: an Antigravity PreInvocation hook that makes sure the
conversation is primed for push (see prime.py). Pure stdlib and cheap, since it
runs before every model invocation.

A hook process does not get the language-server token, so it cannot prime by
itself. Instead it injects a run_command step that runs `bridge-mcp --prime`
in the agent's own shell, where the token exists.
"""
import os
import re
import json
import time
from typing import Any, Dict, Optional

PRIMED_DIR = os.path.join(os.path.expanduser("~"), ".claude", "chatter-primed")
_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# A failed priming must not be retried before every model invocation.
RETRY_AFTER_SECONDS = 600


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


def decide(hook_input: Dict[str, Any], primed_dir: str = PRIMED_DIR, now: Optional[float] = None) -> Dict[str, Any]:
    conv = hook_input.get("conversationId")
    if not isinstance(conv, str) or not _CONVERSATION_ID.match(conv):
        return {}

    try:
        with open(os.path.join(primed_dir, f"{conv}.json"), "r", encoding="utf-8") as f:
            if _pid_alive(json.load(f).get("bridge_pid")):
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

    workspaces = hook_input.get("workspacePaths")
    cwd = workspaces[0] if isinstance(workspaces, list) and workspaces else os.getcwd()
    return {
        "injectSteps": [{
            "toolCall": {
                "name": "run_command",
                "args": {
                    "CommandLine": "bridge-mcp --prime",
                    "Cwd": cwd,
                    "WaitMsBeforeAsync": 5000,
                    "toolAction": "Priming claude-chatter push so messages can wake this session",
                    "toolSummary": "Prime claude-chatter push",
                },
            }
        }]
    }


def run(stdin_text: str) -> str:
    try:
        hook_input = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        hook_input = {}
    return json.dumps(decide(hook_input))
