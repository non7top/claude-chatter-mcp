"""
Claude Code messaging protocol: session discovery (~/.claude/sessions),
Unix-domain-socket transport (auth + message framing, ack), and the
send/receive primitives used to talk to other Claude Code / Antigravity
sessions.

This module has no knowledge of MCP, stdio, or the CLI - it is the
reusable core that both the MCP entry point (mcp_server.py) and the
standalone daemon entry point (standalone.py) build on top of.
"""
import os
import sys
import re
import json
import glob
import uuid
import stat
import time
import string
import sqlite3
import hashlib
import secrets
import asyncio
import logging
import psutil
from typing import Any, Callable, Dict, Optional

from .storage import MessageStore, ResponseStore

logger = logging.getLogger("SocketBridgeMCP")


class ClaudeMessagingProtocol:
    def __init__(
        self,
        bridge_socket_path: Optional[str] = None,
        session_name: Optional[str] = None,
        on_inbound_message: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        db_path: Optional[str] = None,
        enable_storage: bool = True,
        session_id: Optional[str] = None,
        as_bridge: bool = True,
        read_only: bool = False
    ):
        """
        on_inbound_message(method, params), if given, is invoked whenever a real
        inbound message frame is processed - the hook a caller uses to forward
        activity onward (e.g. as an MCP notification). Purely optional: this
        class has no idea what, if anything, is listening on the other end.
        """
        self.pid = os.getpid()
        self.home_dir = os.path.expanduser("~")
        self.sessions_dir = os.path.join(self.home_dir, ".claude", "sessions")
        self.on_inbound_message = on_inbound_message
        self.as_bridge = as_bridge
        self.read_only = read_only

        env_name = os.environ.get("CHATTER_SESSION_NAME") or os.environ.get("AGY_SESSION_NAME")
        self.session_name = session_name or env_name or self._derive_default_session_name()

        env_socket = os.environ.get("CHATTER_BRIDGE_SOCKET") or os.environ.get("AGY_MCP_BRIDGE_SOCKET")
        if bridge_socket_path:
            self.bridge_socket_path = bridge_socket_path
        elif env_socket:
            self.bridge_socket_path = env_socket
        else:
            self.bridge_socket_path = self._get_default_socket_path(self.pid, self.session_name)

        if enable_storage:
            self.db_path = db_path or os.environ.get("CHATTER_DB_PATH") or os.path.join(self.home_dir, ".claude", "chatter-messages.db")
            self.store = MessageStore(self.db_path, read_only=self.read_only)
            if not self.read_only:
                self.store.prune()
            if session_id:
                self.session_id = session_id
            elif getattr(self, "_is_clashing", False):
                self.session_id = str(uuid.uuid4())
            else:
                existing_id = self.store.resolve_session_id(self.session_name)
                if existing_id:
                    self.session_id = existing_id
                else:
                    persisted_name = self.read_persisted_session_name()
                    if not session_name or session_name == persisted_name:
                        self.session_id = self._derive_session_id()
                    else:
                        self.session_id = str(uuid.uuid4())
            if not self.read_only:
                self.store.ensure_session(self.session_name, cwd_digest=self._cwd_digest(), session_id=self.session_id)
                if self.as_bridge and not getattr(self, "_is_clashing", False) and not session_name and not env_name:
                    self.persist_default_session_name(self.session_name)
                    self.persist_session_id(self.session_id)
            initial_messages = self.store.load_session_messages(self.session_id or self.session_name)
            self.response_store = ResponseStore(
                store=self.store,
                session_name=self.session_name,
                session_id=self.session_id,
                cwd_digest=self._cwd_digest(),
                initial=initial_messages
            )
        else:
            self.db_path = None
            self.store = None
            if session_id:
                self.session_id = session_id
            elif getattr(self, "_is_clashing", False):
                self.session_id = str(uuid.uuid4())
            elif not session_name:
                self.session_id = self._derive_session_id()
            else:
                self.session_id = str(uuid.uuid4())
            if not self.read_only and self.as_bridge and not getattr(self, "_is_clashing", False) and not session_name and not env_name:
                self.persist_default_session_name(self.session_name)
                self.persist_session_id(self.session_id)
            self.response_store = {}
        self.active_peers: Dict[str, Dict[str, Any]] = {}

    @property
    def persistence_error(self) -> Optional[str]:
        if self.store and self.store.is_disabled():
            return self.store.last_error or "MessageStore is disabled"
        return None

    @staticmethod
    def _find_project_path() -> str:
        # A plugin-launched server runs in the plugin's install directory, so
        # the agy process above it is the only place the project path lives.
        try:
            agy = next((p for p in psutil.Process().parents() if p.name() == "agy"), None)
            if agy is not None:
                return agy.cwd()
        except psutil.Error:
            pass
        return os.getcwd()

    @staticmethod
    def read_persisted_session_name(project_path: Optional[str] = None) -> str:
        path = project_path or ClaudeMessagingProtocol._find_project_path()
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
        name_path = os.path.join(os.path.expanduser("~"), ".claude", "chatter-session-names", f"{digest}.name")
        try:
            with open(name_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @classmethod
    def resolve_session_name(
        cls,
        explicit_name: Optional[str] = None,
        project_path: Optional[str] = None,
        db_path: Optional[str] = None
    ) -> str:
        if explicit_name:
            return explicit_name
        env_name = os.environ.get("CHATTER_SESSION_NAME") or os.environ.get("AGY_SESSION_NAME")
        if env_name:
            return env_name

        path = project_path or cls._find_project_path()
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]

        sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
        live_candidates = set()
        if os.path.exists(sessions_dir):
            for desc_file in glob.glob(os.path.join(sessions_dir, "bridge-*.json")):
                try:
                    with open(desc_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    raw_pid = data.get("pid")
                    try:
                        pid = int(raw_pid)
                    except (TypeError, ValueError):
                        pid = None
                    if not cls._static_is_pid_alive(pid):
                        continue
                    file_digest = data.get("cwdDigest")
                    if not file_digest:
                        file_cwd = data.get("projectPath") or data.get("cwd")
                        if file_cwd:
                            file_digest = hashlib.sha1(file_cwd.encode("utf-8")).hexdigest()[:8]
                    if file_digest == digest:
                        sname = data.get("name")
                        if sname:
                            live_candidates.add(sname)
                except Exception:
                    pass

        if len(live_candidates) == 1:
            return next(iter(live_candidates))
        elif len(live_candidates) > 1:
            candidates_str = ", ".join(sorted(live_candidates))
            sys.stderr.write(
                f"Multiple live bridge sessions found for this workspace ({candidates_str}). "
                f"Please specify --name to disambiguate.\n"
            )
            return ""

        db = db_path or os.environ.get("CHATTER_DB_PATH") or os.path.join(os.path.expanduser("~"), ".claude", "chatter-messages.db")
        if os.path.exists(db):
            try:
                uri = f"file:{os.path.abspath(db)}?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
                    cur = conn.execute(
                        "SELECT current_name FROM sessions WHERE cwd_digest = ? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                        (digest,)
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0]
            except Exception:
                pass

        persisted = cls.read_persisted_session_name(path)
        if persisted:
            return persisted

        return ""

    def _project_path(self) -> str:
        return self._find_project_path()

    def _cwd_digest(self) -> str:
        return hashlib.sha1(self._project_path().encode("utf-8")).hexdigest()[:8]

    def _persisted_name_path(self, cwd_digest: str) -> str:
        d = os.path.join(self.home_dir, ".claude", "chatter-session-names")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return os.path.join(d, f"{cwd_digest}.name")

    def _persisted_id_path(self, cwd_digest: str) -> str:
        d = os.path.join(self.home_dir, ".claude", "chatter-session-names")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return os.path.join(d, f"{cwd_digest}.id")

    def _derive_session_id(self) -> str:
        id_path = self._persisted_id_path(self._cwd_digest())
        try:
            with open(id_path, "r", encoding="utf-8") as f:
                sid = f.read().strip()
                if sid:
                    return sid
        except OSError:
            pass
        sid = str(uuid.uuid4())
        if self.as_bridge and not self.read_only:
            try:
                with open(id_path, "w", encoding="utf-8") as f:
                    f.write(sid)
            except OSError:
                pass
        return sid

    def _derive_default_session_name(self) -> str:
        """
        Returns the session name persisted for this project path (one small
        file per path digest, so unrelated projects never contend on a file).
        With nothing stored, generates "<basename>-<2 random
        alphanumerics>" and persists it, so the name survives the restarts that
        happen on every MCP host reload. rename_session overwrites the stored
        value.

        If another live session already holds the stored name (two sessions
        in one project), this process gets a fresh random suffix that is not
        persisted, so the two never share a socket.
        """
        try:
            with open(self._persisted_name_path(self._cwd_digest()), "r", encoding="utf-8") as f:
                persisted = f.read().strip()
        except OSError:
            persisted = ""
        if not persisted:
            persisted = self._random_session_name()
            if self.as_bridge and not self.read_only:
                self.persist_default_session_name(persisted)
        if self._session_name_in_use(persisted, wait_if_same_cwd=self.as_bridge):
            self._is_clashing = True
            return self._random_session_name()
        return persisted

    def _random_session_name(self) -> str:
        basename = re.sub(r"[^A-Za-z0-9._-]", "-", os.path.basename(self._project_path())) or "root"
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(2))
        return f"{basename}-{suffix}"

    @staticmethod
    def _static_is_pid_alive(pid: Optional[int]) -> bool:
        if not pid or not isinstance(pid, int):
            return False
        try:
            proc = psutil.Process(pid)
            return proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        except Exception:
            return False

    def _is_pid_alive(self, pid: Optional[int]) -> bool:
        return self._static_is_pid_alive(pid)

    def _session_name_in_use(
        self,
        name: str,
        wait_if_same_cwd: bool = False,
        max_wait: float = 1.5,
        poll_interval: float = 0.1
    ) -> bool:
        start_time = time.time()
        while True:
            in_use_by_other = False
            same_cwd = False
            for path in glob.glob(os.path.join(self.sessions_dir, "*.json")):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    continue
                pid = meta.get("pid")
                if meta.get("name") == name and pid != self.pid and self._is_pid_alive(pid):
                    in_use_by_other = True
                    desc_proj = meta.get("projectPath") or meta.get("cwd") or ""
                    desc_digest = meta.get("cwdDigest") or ""
                    if (desc_digest and desc_digest == self._cwd_digest()) or (
                        desc_proj and os.path.realpath(desc_proj) == os.path.realpath(self._project_path())
                    ):
                        same_cwd = True
                    break

            if not in_use_by_other:
                return False

            if not wait_if_same_cwd or not same_cwd or (time.time() - start_time) >= max_wait:
                return True

            time.sleep(poll_interval)

    def persist_default_session_name(self, name: str):
        """
        Remembers `name` as this workspace's default session name for future
        process starts (see _derive_default_session_name). Only meaningful
        for the cwd-derived default - an explicit --name/CHATTER_SESSION_NAME
        override always wins regardless of what's persisted here.
        """
        try:
            with open(self._persisted_name_path(self._cwd_digest()), "w", encoding="utf-8") as f:
                f.write(name)
            logger.info(f"Persisted '{name}' as this workspace's default session name.")
        except OSError as e:
            logger.error(f"Failed to persist session name '{name}': {e}")

    def persist_session_id(self, session_id: str):
        try:
            with open(self._persisted_id_path(self._cwd_digest()), "w", encoding="utf-8") as f:
                f.write(session_id)
            logger.info(f"Persisted '{session_id}' as this workspace's session id.")
        except OSError as e:
            logger.error(f"Failed to persist session id '{session_id}': {e}")

    def _get_default_socket_path(self, pid: int, session_name: Optional[str] = None) -> str:
        sname = (session_name or getattr(self, "session_name", "bridge")).replace("/", "-")
        try:
            uid = os.getuid()
            cc_socks_dir = f"/run/user/{uid}/cc-socks"
            if os.path.exists(cc_socks_dir) and os.access(cc_socks_dir, os.W_OK):
                return os.path.join(cc_socks_dir, f"bridge-{sname}.sock")

            fallback_dir = "/tmp/cc-socks"
            os.makedirs(fallback_dir, exist_ok=True)
            return os.path.join(fallback_dir, f"bridge-{sname}.sock")
        except Exception:
            pass
        return f"/tmp/bridge_{sname}.sock"

    async def verify_and_purge_sessions(self) -> Dict[str, Dict[str, Any]]:
        """
        Scans workspace configurations, checks if tracking PIDs are alive,
        and systematically cleans up orphaned or dead sockets and key files.
        """
        logger.info("Running proactive session structural analysis...")
        if not os.path.exists(self.sessions_dir):
            logger.warning(f"Sessions directory does not exist: {self.sessions_dir}")
            self.active_peers = {}
            return {}

        current_sessions: Dict[str, Dict[str, Any]] = {}
        json_files = glob.glob(os.path.join(self.sessions_dir, "*.json"))
        purged_count = 0

        for file_path in json_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)

                pid = meta.get("pid")
                sock_path = meta.get("messagingSocketPath")
                name = meta.get("name", f"session-{pid}")
                session_id = meta.get("sessionId", "")
                cwd = meta.get("cwd", "")

                # Check process lifecycle
                if pid and self._is_pid_alive(pid):
                    # Check for accompanying auth key file
                    key_pattern = os.path.join(self.sessions_dir, f"{pid}.*.key")
                    discovered_keys = glob.glob(key_pattern)

                    token = ""
                    if discovered_keys:
                        try:
                            with open(discovered_keys[0], "r", encoding="utf-8") as kf:
                                key_raw = kf.read().strip()
                                try:
                                    key_data = json.loads(key_raw)
                                    if isinstance(key_data, dict) and "peerToken" in key_data:
                                        token = key_data["peerToken"]
                                    else:
                                        token = key_raw
                                except json.JSONDecodeError:
                                    token = key_raw
                        except Exception as kerr:
                            logger.error(f"Failed to read key file {discovered_keys[0]}: {kerr}")

                    current_sessions[name] = {
                        "name": name,
                        "pid": pid,
                        "sessionId": session_id,
                        "socket": sock_path,
                        "token": token,
                        "cwd": cwd,
                        "kind": meta.get("kind", "interactive"),
                        "updatedAt": meta.get("updatedAt"),
                        "status": meta.get("status", "active"),
                        "jobId": meta.get("jobId"),
                        "parkedJobId": meta.get("parkedJobId"),
                    }
                else:
                    # Target process is dead; purge stale descriptor and key files safely
                    logger.warning(f"Purging dead session files for PID {pid}")
                    try:
                        os.remove(file_path)
                        purged_count += 1
                    except OSError as e:
                        logger.error(f"Failed to remove stale session file {file_path}: {e}")

                    # Also purge associated key files for dead PID
                    if pid:
                        key_pattern = os.path.join(self.sessions_dir, f"{pid}.*.key")
                        for key_file in glob.glob(key_pattern):
                            try:
                                os.remove(key_file)
                                logger.info(f"Purged stale key file: {key_file}")
                            except OSError as e:
                                logger.error(f"Failed to remove stale key file {key_file}: {e}")
            except Exception as e:
                logger.error(f"Error checking session file {file_path}: {e}")

        self.active_peers = current_sessions
        if self.store:
            self.store.prune()
        logger.info(f"Session analysis complete. Active peers: {len(current_sessions)}, Purged: {purged_count}")
        return self.active_peers

    @staticmethod
    def redact_peers(peers: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Copy of `peers` without auth tokens, safe to hand to an agent or print."""
        return {n: {k: v for k, v in p.items() if k != "token"} for n, p in peers.items()}

    def _resolve_session(self, session_identifier: str) -> Optional[Dict[str, Any]]:
        if not self.active_peers:
            return None

        # Direct match by name
        if session_identifier in self.active_peers:
            return self.active_peers[session_identifier]

        # Match by PID (str or int)
        for peer in self.active_peers.values():
            if str(peer.get("pid")) == str(session_identifier):
                return peer

        # Match by SessionID
        for peer in self.active_peers.values():
            if peer.get("sessionId") == session_identifier:
                return peer

        # Substring / case-insensitive match on name
        session_lower = session_identifier.lower()
        for name, peer in self.active_peers.items():
            if session_lower in name.lower():
                return peer

        # If user passed "default", "first", or "active", return first active session
        if session_identifier.lower() in ["default", "first", "active", "any"]:
            return next(iter(self.active_peers.values()))

        return None

    async def send_to_claude(
        self,
        session_identifier: str,
        message_content: str
    ) -> Dict[str, Any]:
        """
        Establishes an out-bound wire pipe to a verified target session and dispatches
        a message. Fire-and-forget: if the target sends a real reply, it arrives later
        as its own inbound frame (see handle_inbound_client) - there is no in_reply_to
        correlation in the wire protocol, so this does not (and cannot honestly) wait
        for or return that reply synchronously.
        """
        await self.verify_and_purge_sessions()

        peer = self._resolve_session(session_identifier)
        if not peer:
            logger.error(f"Target session '{session_identifier}' is not reachable.")
            return {
                "success": False,
                "error": f"Target session '{session_identifier}' not found among active sessions: {list(self.active_peers.keys())}"
            }

        socket_path = peer.get("socket")

        if not socket_path or not os.path.exists(socket_path):
            logger.error(f"Socket path for '{peer['name']}' does not exist: {socket_path}")
            return {
                "success": False,
                "error": f"Socket path '{socket_path}' for session '{peer['name']}' does not exist on disk."
            }

        # Wire format reverse-engineered from a real Claude Code SendMessage
        # frame (captured directly off the socket): no separate auth frame,
        # a "from" field holding this sender's own "uds:<socket path>" URI
        # (not a bare name), a msgV version field, and the message content
        # itself wrapped in a <cross-session-message from=... from-name=...
        # from-mode=...> tag - the receiving side's pretty, reply-able
        # rendering keys off that wrapper being present in the content, not
        # off any separate top-level metadata field. A prior version omitted
        # all of this and sent a bare "sender" name instead of "from" -
        # real Claude Code sessions still buffered that content correctly,
        # but rendered it as plain unwrapped text with no reply-to address,
        # and (unconfirmed but consistent with every observed case) reported
        # send failure back to the sender despite the content having arrived.
        msg_id = str(uuid.uuid4())
        from_uri = f"uds:{self.bridge_socket_path}"
        wrapped_content = (
            f'<cross-session-message from="{from_uri}" from-name="{self.session_name}" '
            f'from-mode="prompting">\n{message_content}\n</cross-session-message>'
        )
        message_frame = {
            "msgV": 1,
            "msg_id": msg_id,
            "type": "user",
            "message": {"role": "user", "content": wrapped_content},
            "priority": "next",
            "from": from_uri
        }

        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write((json.dumps(message_frame) + "\n").encode("utf-8"))
            await writer.drain()

            try:
                ack_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if ack_line:
                    logger.info(f"Received instant ACK from {peer['name']}: {ack_line.decode('utf-8').strip()}")
            except (asyncio.TimeoutError, Exception):
                pass

            writer.close()
            await writer.wait_closed()

            entry = {
                "direction": "outbound",
                "status": "dispatched",
                "msg_id": msg_id,
                "target_session": peer["name"],
                "target_pid": peer["pid"],
                "content": message_content,
                "response": "",
                "timestamp": time.time()
            }
            self.response_store[msg_id] = entry
            logger.info(f"Successfully dispatched message {msg_id} to {peer['name']}")

            return {
                "success": True,
                "msg_id": msg_id,
                "target": peer["name"],
                "status": "dispatched",
                "detail": entry
            }

        except Exception as e:
            logger.error(f"IPC injection crash sending to {socket_path}: {e}")
            return {
                "success": False,
                "error": f"Failed to send message over Unix socket to {socket_path}: {e}"
            }

    def _compute_pid_domain(self, pid: int) -> str:
        """
        Builds the pidDomain field the way real Claude Code sessions do:
        "linux:<contents of /etc/machine-id>:pid:[<this pid's PID-namespace
        inode, from /proc/<pid>/ns/pid>]" - verified by comparing a real
        session's descriptor against this host's actual /etc/machine-id and
        /proc/<pid>/ns/pid. A prior version fabricated "linux:local:pid:[pid]"
        (the literal PID, not its namespace inode, and a placeholder instead
        of the real machine id) - plausibly why native cross-session
        SendMessage could resolve this bridge by name but fail to actually
        deliver to it. Falls back to that placeholder shape only if either
        file is unreadable (e.g. non-Linux).
        """
        try:
            with open("/etc/machine-id", "r", encoding="utf-8") as f:
                machine_id = f.read().strip()
        except OSError:
            machine_id = "local"
        try:
            ns_link = os.readlink(f"/proc/{pid}/ns/pid")  # "pid:[4026532221]"
            ns_part = ns_link.split(":", 1)[1] if ":" in ns_link else f"[{pid}]"
        except OSError:
            ns_part = f"[{pid}]"
        return f"linux:{machine_id}:pid:{ns_part}"

    def _compute_proc_start(self, pid: int) -> str:
        """
        Real Claude Code descriptors put /proc/<pid>/stat's 22nd field (process
        start time in clock ticks since boot - the kernel's own anti-PID-reuse
        value) in procStart, not the pid itself - verified against a real
        session's descriptor (its procStart matched that field exactly; the
        pid did not). Falls back to str(pid) only if /proc is unreadable.
        """
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                # Field 2 (comm) is parenthesized and may itself contain
                # spaces/parens, so split after its closing paren rather than
                # by naive whitespace splitting from the start.
                after_comm = f.read().rsplit(")", 1)[1].split()
                return after_comm[19]  # fields are 1-indexed; index from field 3 onward
        except (OSError, IndexError):
            return str(pid)

    def register_session_descriptor(self, session_name: str = "bridge", kind: str = "bg"):
        """
        Registers a session descriptor in ~/.claude/sessions/ so that surrounding
        Claude Code processes can discover and message this bridge via ListAgents.
        """
        if not os.path.exists(self.sessions_dir):
            try:
                os.makedirs(self.sessions_dir, exist_ok=True)
            except OSError as e:
                logger.error(f"Failed to create sessions directory: {e}")
                return

        self.registered_pid = os.getpid()
        self.registered_session_id = str(uuid.uuid4())
        self.registered_token = uuid.uuid4().hex
        now = int(time.time() * 1000)

        sanitized_name = session_name.replace("/", "-")
        self.session_json_path = os.path.join(self.sessions_dir, f"{self.registered_pid}.json")
        self.session_key_path = os.path.join(self.sessions_dir, f"{self.registered_pid}.{self.registered_token[:16]}.key")
        self.named_descriptor_path = os.path.join(self.sessions_dir, f"bridge-{sanitized_name}.json")

        session_data = {
            "pid": self.registered_pid,
            "sessionId": self.registered_session_id,
            "cwd": os.getcwd(),
            "projectPath": self._project_path(),
            "cwdDigest": self._cwd_digest(),
            "startedAt": now,
            "procStart": self._compute_proc_start(self.registered_pid),
            "version": "2.1.282",
            "peerProtocol": 1,
            "peerFeatures": ["notify_idle", "reply_across_default_dirs", "artifact_yield"],
            "kind": kind,
            "entrypoint": "cli",
            "pidDomain": self._compute_pid_domain(self.registered_pid),
            "messagingSocketPath": self.bridge_socket_path,
            "name": session_name,
            "nameSource": "user",
            "nameSince": now,
            "status": "idle",
            "updatedAt": now,
            "statusUpdatedAt": now
        }

        key_data = {
            "peerToken": self.registered_token,
            "procStart": session_data["procStart"],
            "pidDomain": session_data["pidDomain"]
        }

        try:
            with open(self.session_json_path, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2)
            with open(self.named_descriptor_path, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2)
            with open(os.open(self.session_key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                json.dump(key_data, f, indent=2)
            logger.info(f"Registered bridge session descriptor '{session_name}' (PID {self.registered_pid}) in {self.sessions_dir}")
        except Exception as e:
            logger.error(f"Failed to register session descriptor: {e}")

    def cleanup_session_descriptor(self):
        """
        Cleans up the bridge's registered session descriptor and key files on shutdown.
        """
        for p in [getattr(self, "session_json_path", None), getattr(self, "session_key_path", None), getattr(self, "named_descriptor_path", None)]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    logger.info(f"Cleaned up session file: {p}")
                except OSError as e:
                    logger.error(f"Failed to remove descriptor file {p}: {e}")

    async def handle_inbound_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        Manages inbound response tracking. Listens for external peer confirmations
        and caches payloads for immediate lookup.
        """
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    payload = json.loads(line_str)
                    frame_type = payload.get("type")

                    if frame_type == "auth":
                        logger.info("Inbound peer authenticated successfully.")
                        continue

                    msg_id = payload.get("msg_id", f"inbound_{uuid.uuid4()}")

                    msg_obj = payload.get("message", {})
                    if isinstance(msg_obj, dict):
                        content_val = msg_obj.get("content", payload.get("content", ""))
                    else:
                        content_val = payload.get("content", str(msg_obj))

                    # Real Claude Code senders don't send a bare "sender" name -
                    # they embed identity as from-name inside a
                    # <cross-session-message from=... from-name=... from-mode=...>
                    # wrapper around the actual text, with a top-level "from"
                    # (a "uds:<socket path>" URI) as the only other identity
                    # signal. Only our own older frames (and other bridges
                    # still using the pre-fix format) send a plain "sender"
                    # field, so that's tried first without breaking anything.
                    sender = payload.get("sender")
                    wrapper_match = re.match(
                        r'^<cross-session-message\s+([^>]*)>\n(.*)\n</cross-session-message>$',
                        content_val, re.DOTALL
                    )
                    if wrapper_match:
                        attrs, inner_text = wrapper_match.groups()
                        if sender is None:
                            name_match = re.search(r'from-name="([^"]*)"', attrs)
                            sender = name_match.group(1) if name_match else payload.get("from", "unknown")
                        content_val = inner_text
                    if sender is None:
                        sender = payload.get("from", "unknown")

                    # Cache response
                    self.response_store[msg_id] = {
                        "direction": "inbound",
                        "read": False,
                        "status": payload.get("status", "received"),
                        "sender": sender,
                        "content": content_val,
                        "payload": payload,
                        "timestamp": time.time()
                    }
                    logger.info(f"Buffered inbound response frame: {msg_id}")

                    if self.on_inbound_message:
                        self.on_inbound_message("notifications/message", {
                            "msg_id": msg_id,
                            "sender": sender,
                            "content": content_val,
                            "timestamp": time.time()
                        })

                    # No reply on purpose: Claude Code's sender reports "Failed to
                    # send" if the peer writes anything back, even though the
                    # message was buffered.

                except json.JSONDecodeError:
                    logger.error("Received invalid JSON payload over inbound bridge socket")
                except Exception as ex:
                    logger.error(f"Error processing inbound frame payload: {ex}")
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            logger.info("Inbound peer connection closed.")
        except Exception as e:
            logger.error(f"Error handling inbound traffic: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def get_unread_messages(self, mark_read: bool = True) -> Dict[str, Dict[str, Any]]:
        """
        Returns currently-unread inbound messages (oldest first), the actual
        "check my inbox" affordance for the async-mailbox model: since pushing to the
        connected host is best effort, whoever is checking
        needs a way to see only what's new since they last looked, rather than
        re-seeing the same history or having to track msg_ids themselves.
        Marks the returned messages as read unless mark_read is False (to
        peek without consuming).
        """
        unread = {
            msg_id: entry
            for msg_id, entry in self.response_store.items()
            if entry.get("direction") == "inbound" and not entry.get("read", False)
        }
        unread = dict(sorted(unread.items(), key=lambda kv: kv[1].get("timestamp", 0)))
        if mark_read:
            to_mark = []
            for msg_id, entry in unread.items():
                entry["read"] = True
                to_mark.append(msg_id)
            if to_mark and isinstance(self.response_store, ResponseStore):
                self.response_store.mark_read(to_mark)
        return unread

    def rename_session_in_store(self, old_name: str, new_name: str):
        """
        Renames the session in SQLite storage, migrating message histories.
        When renaming onto a session name with existing message history in
        storage, duplicate msg_ids retain the renamed session's copy.
        """
        if self.store:
            self.session_id = self.store.rename_session(
                old_name,
                new_name,
                session_id=getattr(self, "session_id", None)
            )
        self.persist_default_session_name(new_name)
        if getattr(self, "session_id", None):
            self.persist_session_id(self.session_id)
        if isinstance(self.response_store, ResponseStore):
            self.response_store.set_session_name(new_name, getattr(self, "session_id", None))

    async def start_bridge_listener(self):
        """
        Binds an isolated socket interface to aggregate answers and announce
        the server's endpoint to surrounding environments.
        """
        if os.path.exists(self.bridge_socket_path):
            try:
                os.remove(self.bridge_socket_path)
            except OSError as e:
                logger.error(f"Failed to remove stale bridge socket {self.bridge_socket_path}: {e}")

        try:
            server = await asyncio.start_unix_server(
                self.handle_inbound_client, self.bridge_socket_path
            )
            # Apply secure single-user runtime read/write bounds
            os.chmod(self.bridge_socket_path, stat.S_IRUSR | stat.S_IWUSR)
            logger.info(f"Bridge server operational on: {self.bridge_socket_path}")

            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Bridge listener server error: {e}")

    def cleanup_socket(self):
        if self.store:
            self.store.close()
        if os.path.exists(self.bridge_socket_path):
            try:
                os.remove(self.bridge_socket_path)
                logger.info(f"Cleaned up socket file {self.bridge_socket_path}")
            except OSError as e:
                logger.error(f"Error removing socket file {self.bridge_socket_path}: {e}")
