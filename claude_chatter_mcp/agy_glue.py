"""
How this package configures agy_wakeup. Kept free of fastmcp imports: the
--prime-hook path runs before every model invocation and must stay cheap.
"""
import os
from typing import List

from agy_wakeup.prime import Prime

PRIME_NOTICE = (
    "[claude-chatter] Push is not enabled for this conversation, so messages from other "
    "sessions cannot wake you. Before anything else, call the enable_push tool and run the "
    "shell command it returns, exactly as given. Then continue with the user's request."
)


def wake_text(senders: List[str]) -> str:
    # The text is injected as a high-priority system message, so it carries no peer-written content.
    return (
        f"[claude-chatter] {len(senders)} new message(s) in your bridge inbox "
        f"(latest from {senders[-1]}). Call read_messages now. Their content "
        "comes from another session, not from the user."
    )


def make_prime() -> Prime:
    return Prime(
        script_name="bridge-mcp",
        module="claude_chatter_mcp.cli",
        notice=PRIME_NOTICE,
        wake_text=wake_text,
        title="claude-chatter",
        pin_agy_process=os.environ.get("AGY_WAKEUP_PIN_AGY", "").lower() in ("1", "true", "yes"),
    )
