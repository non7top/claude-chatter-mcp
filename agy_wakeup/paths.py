"""Filesystem locations and identifiers shared by the hook, the poker and the primer. Stdlib only."""
import os
import re

PRIMED_DIR = os.path.join(os.path.expanduser("~"), ".claude", "chatter-primed")
STABLE_LINK_DIR = os.path.join(os.path.expanduser("~"), ".claude", "chatter-bin")
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
