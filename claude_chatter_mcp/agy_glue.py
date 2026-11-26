"""
How this package configures antigravity_wakeup. Kept free of fastmcp imports: the
--prime-hook path runs before every model invocation and must stay cheap.
"""
import os
from typing import List, Optional

import psutil
from antigravity_wakeup.prime import Prime
from antigravity_wakeup.shell import AgyLocator

SANDBOX_NOTE = (
    "Run it with the terminal sandbox bypassed: the sandbox hides ~/.claude and the bridge's "
    "control socket, so a sandboxed run fails with 'No such file or directory'."
)

PRIME_NOTICE = (
    "[claude-chatter] Push is not enabled for this conversation, so messages from other "
    "sessions cannot wake you. Before anything else, call the enable_push tool and run the "
    "shell command it returns, exactly as given. " + SANDBOX_NOTE + " Then continue with the user's request."
)


def wake_text(senders: List[str]) -> str:
    # The text is injected as a high-priority system message, so it carries no peer-written content.
    return (
        f"[claude-chatter] {len(senders)} new message(s) in your bridge inbox "
        f"(latest from {senders[-1]}). Call read_messages now. Their content "
        "comes from another session, not from the user."
    )


def agy_start_dir() -> Optional[str]:
    """The directory the agy above this process was started in, or None when not under agy."""
    try:
        agy = AgyLocator.find_ancestor()
    except psutil.Error:
        return None
    if agy is None:
        return None
    # environ() is the startup environment, so PWD ignores later chdirs; cwd() is the fallback.
    for get in (lambda: agy.environ().get("PWD"), agy.cwd):
        try:
            path = get()
        except psutil.Error:
            continue
        if path and os.path.isabs(path) and os.path.isdir(path):
            return path
    return None


def resolve_project_path(explicit: Optional[str]) -> Optional[str]:
    """--project-path, then CHATTER_PROJECT_PATH (read by the library), then where agy started."""
    if explicit or os.environ.get("CHATTER_PROJECT_PATH"):
        return explicit
    return agy_start_dir()


def make_prime() -> Prime:
    return Prime(
        script_name="bridge-mcp",
        module="claude_chatter_mcp.cli",
        notice=PRIME_NOTICE,
        wake_text=wake_text,
        title="claude-chatter",
        pin_agy_process=os.environ.get("AGY_WAKEUP_PIN_AGY", "").lower() in ("1", "true", "yes"),
    )
