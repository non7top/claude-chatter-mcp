"""
SQLite message persistence for claude-chatter: preserves inbound and outbound
messages across bridge restarts, tracking read/unread status.
"""
import json
import logging
import os
import sqlite3
import stat
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SocketBridgeMCP")

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        current_name TEXT NOT NULL,
        cwd_digest TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS session_aliases (
        alias_name TEXT NOT NULL,
        session_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (alias_name, session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        msg_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        session_name TEXT NOT NULL,
        cwd_digest TEXT,
        direction TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0,
        sender TEXT,
        target_session TEXT,
        target_pid INTEGER,
        content TEXT,
        status TEXT,
        timestamp REAL NOT NULL,
        entry_json TEXT NOT NULL,
        PRIMARY KEY (session_id, msg_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages (session_id, direction, read)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_name ON messages (session_name, direction, read)",
    "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages (timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_name ON sessions (current_name)",
    "CREATE INDEX IF NOT EXISTS idx_session_aliases_name ON session_aliases (alias_name)",
]

SCHEMA = "\n".join(s + ";" for s in SCHEMA_STATEMENTS)


class MessageStore:
    def __init__(self, db_path: Optional[str] = None, read_only: bool = False):
        if db_path:
            self.db_path = os.path.abspath(os.path.expanduser(db_path))
        else:
            env_path = os.environ.get("CHATTER_DB_PATH")
            if env_path:
                self.db_path = os.path.abspath(os.path.expanduser(env_path))
            else:
                self.db_path = os.path.expanduser("~/.claude/chatter-messages.db")

        self.read_only = read_only
        self._conn: Optional[sqlite3.Connection] = None
        self._disabled = False
        self.last_error: Optional[str] = None
        self._last_prune_time: float = 0.0
        if not self.read_only:
            self._init_db()

    def is_disabled(self) -> bool:
        return self._disabled

    def _init_db(self):
        try:
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            is_new = not os.path.exists(self.db_path)
            conn = self._get_connection()
            if is_new and os.path.exists(self.db_path):
                try:
                    os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass

            cur_old = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages_old'")
            if cur_old.fetchone():
                logger.warning("Found orphaned messages_old table from an interrupted migration; recovering...")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in SCHEMA_STATEMENTS:
                        conn.execute(stmt)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO messages (
                            msg_id, session_id, session_name, cwd_digest, direction, read,
                            sender, target_session, target_pid, content, status,
                            timestamp, entry_json
                        )
                        SELECT
                            m.msg_id,
                            COALESCE(s.session_id, m.session_name),
                            m.session_name,
                            m.cwd_digest,
                            m.direction,
                            m.read,
                            m.sender,
                            m.target_session,
                            m.target_pid,
                            m.content,
                            m.status,
                            m.timestamp,
                            m.entry_json
                        FROM messages_old m
                        LEFT JOIN sessions s ON m.session_name = s.current_name
                        """
                    )
                    conn.execute("DROP TABLE messages_old")
                    conn.execute("PRAGMA user_version = 2;")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
            if cur.fetchone():
                info = conn.execute("PRAGMA table_info(messages)").fetchall()
                col_names = {row["name"] for row in info}
                if "session_id" not in col_names:
                    logger.info("Migrating messages table to schema v2 with stable session_id")
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.execute(SCHEMA_STATEMENTS[0])
                        conn.execute(SCHEMA_STATEMENTS[1])
                        s_cur = conn.execute("SELECT DISTINCT session_name, cwd_digest FROM messages")
                        now = time.time()
                        for s_row in s_cur.fetchall():
                            sname = s_row["session_name"]
                            cdigest = s_row["cwd_digest"] or ""
                            sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, sname))
                            conn.execute(
                                "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, ?)",
                                (sid, sname, cdigest, now, now)
                            )
                            conn.execute(
                                "INSERT OR IGNORE INTO session_aliases VALUES (?, ?, ?)",
                                (sname, sid, now)
                            )
                        conn.execute("ALTER TABLE messages RENAME TO messages_old")
                        for stmt in SCHEMA_STATEMENTS[2:]:
                            conn.execute(stmt)
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO messages (
                                msg_id, session_id, session_name, cwd_digest, direction, read,
                                sender, target_session, target_pid, content, status,
                                timestamp, entry_json
                            )
                            SELECT
                                m.msg_id,
                                COALESCE(s.session_id, m.session_name),
                                m.session_name,
                                m.cwd_digest,
                                m.direction,
                                m.read,
                                m.sender,
                                m.target_session,
                                m.target_pid,
                                m.content,
                                m.status,
                                m.timestamp,
                                m.entry_json
                            FROM messages_old m
                            LEFT JOIN sessions s ON m.session_name = s.current_name
                            """
                        )
                        conn.execute("DROP TABLE messages_old")
                        conn.execute("PRAGMA user_version = 2;")
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                else:
                    with conn:
                        for stmt in SCHEMA_STATEMENTS:
                            conn.execute(stmt)
                        conn.execute("PRAGMA user_version = 2;")
            else:
                with conn:
                    for stmt in SCHEMA_STATEMENTS:
                        conn.execute(stmt)
                    conn.execute("PRAGMA user_version = 2;")
        except Exception as e:
            logger.error(f"Failed to initialize SQLite store at {self.db_path}: {e}")
            sys.stderr.write(f"Warning: Failed to initialize SQLite store at {self.db_path}: {e}\n")
            self._disabled = True
            self.last_error = str(e)

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.read_only:
                try:
                    uri = f"file:{os.path.abspath(self.db_path)}?mode=ro"
                    self._conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
                    self._conn.row_factory = sqlite3.Row
                except Exception as e:
                    logger.error(f"Failed to open SQLite store in read-only mode at {self.db_path}: {e}")
                    self._disabled = True
                    self.last_error = str(e)
                    raise
            else:
                self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                with self._conn:
                    self._conn.execute("PRAGMA journal_mode=WAL;")
                    self._conn.execute("PRAGMA busy_timeout=5000;")
                    self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    def ensure_session(
        self,
        session_name: str,
        cwd_digest: str = "",
        session_id: Optional[str] = None
    ) -> str:
        if self._disabled or self.read_only:
            return session_id or session_name
        now = time.time()
        try:
            conn = self._get_connection()
            with conn:
                if session_id:
                    cur = conn.execute("SELECT session_id, current_name FROM sessions WHERE session_id = ?", (session_id,))
                    row = cur.fetchone()
                    if row:
                        if row["current_name"] != session_name:
                            conn.execute(
                                "INSERT OR REPLACE INTO session_aliases (alias_name, session_id, created_at) VALUES (?, ?, ?)",
                                (row["current_name"], session_id, now)
                            )
                            conn.execute("DELETE FROM session_aliases WHERE alias_name = ?", (session_name,))
                            conn.execute(
                                "INSERT INTO session_aliases (alias_name, session_id, created_at) VALUES (?, ?, ?)",
                                (session_name, session_id, now)
                            )
                            conn.execute(
                                "UPDATE sessions SET current_name = ?, cwd_digest = COALESCE(NULLIF(?, ''), cwd_digest), updated_at = ? WHERE session_id = ?",
                                (session_name, cwd_digest, now, session_id)
                            )
                        return session_id

                    cur = conn.execute("SELECT session_id FROM sessions WHERE current_name = ?", (session_name,))
                    existing = cur.fetchone()
                    if existing:
                        return existing["session_id"]

                    conn.execute("DELETE FROM session_aliases WHERE alias_name = ?", (session_name,))
                    conn.execute(
                        "INSERT INTO sessions (session_id, current_name, cwd_digest, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (session_id, session_name, cwd_digest, now, now)
                    )
                    conn.execute(
                        "INSERT INTO session_aliases (alias_name, session_id, created_at) VALUES (?, ?, ?)",
                        (session_name, session_id, now)
                    )
                    return session_id

                cur = conn.execute("SELECT session_id FROM sessions WHERE current_name = ?", (session_name,))
                row = cur.fetchone()
                if row:
                    return row["session_id"]

                sid = str(uuid.uuid4())
                conn.execute("DELETE FROM session_aliases WHERE alias_name = ?", (session_name,))
                conn.execute(
                    "INSERT INTO sessions (session_id, current_name, cwd_digest, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (sid, session_name, cwd_digest, now, now)
                )
                conn.execute(
                    "INSERT INTO session_aliases (alias_name, session_id, created_at) VALUES (?, ?, ?)",
                    (session_name, sid, now)
                )
                return sid
        except Exception as e:
            logger.error(f"Failed to ensure session {session_name}: {e}")
            return session_id or session_name

    def resolve_session_id(self, name_or_id: str) -> Optional[str]:
        if self._disabled or not name_or_id:
            return None
        try:
            conn = self._get_connection()
            cur = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (name_or_id,))
            row = cur.fetchone()
            if row:
                return row["session_id"]

            cur = conn.execute("SELECT session_id FROM sessions WHERE current_name = ?", (name_or_id,))
            row = cur.fetchone()
            if row:
                return row["session_id"]

            cur = conn.execute(
                "SELECT session_id FROM session_aliases WHERE alias_name = ? ORDER BY created_at DESC LIMIT 1",
                (name_or_id,)
            )
            row = cur.fetchone()
            if row:
                return row["session_id"]
        except Exception as e:
            logger.error(f"Failed to resolve session id for {name_or_id}: {e}")
        return None

    def resolve_session_by_cwd_digest(self, cwd_digest: str) -> Optional[str]:
        if self._disabled or not cwd_digest:
            return None
        try:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT current_name FROM sessions WHERE cwd_digest = ? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                (cwd_digest,)
            )
            row = cur.fetchone()
            if row and row["current_name"]:
                return row["current_name"]
        except Exception as e:
            logger.error(f"Failed to resolve session by cwd_digest {cwd_digest}: {e}")
        return None

    def save_message(
        self,
        msg_id: str,
        entry: Dict[str, Any],
        session_name: str,
        cwd_digest: str = "",
        session_id: Optional[str] = None
    ):
        if self._disabled or self.read_only:
            return
        now = time.time()
        if now - self._last_prune_time >= 3600:
            self.prune()
        try:
            sess_id = session_id or self.ensure_session(session_name, cwd_digest)
            conn = self._get_connection()
            direction = entry.get("direction", "inbound")
            read_val = 1 if entry.get("read", False) else 0
            sender = entry.get("sender")
            target_session = entry.get("target_session")
            target_pid = entry.get("target_pid")
            content = entry.get("content", "")
            status = entry.get("status", "")
            ts = float(entry.get("timestamp", now))
            entry_json = json.dumps(entry)

            with conn:
                conn.execute(
                    """
                    INSERT INTO messages (
                        msg_id, session_id, session_name, cwd_digest, direction, read,
                        sender, target_session, target_pid, content, status,
                        timestamp, entry_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, msg_id) DO UPDATE SET
                        session_name = excluded.session_name,
                        cwd_digest = excluded.cwd_digest,
                        direction = excluded.direction,
                        read = excluded.read,
                        sender = excluded.sender,
                        target_session = excluded.target_session,
                        target_pid = excluded.target_pid,
                        content = excluded.content,
                        status = excluded.status,
                        timestamp = excluded.timestamp,
                        entry_json = excluded.entry_json
                    """,
                    (
                        msg_id, sess_id, session_name, cwd_digest, direction, read_val,
                        sender, target_session, target_pid, content, status,
                        ts, entry_json
                    )
                )
        except Exception as e:
            logger.error(f"Failed to persist message {msg_id} for {session_name}: {e}")

    def mark_read(self, session_name_or_id: str, msg_ids: List[str]):
        if self._disabled or self.read_only or not msg_ids:
            return
        try:
            sess_id = self.resolve_session_id(session_name_or_id)
            conn = self._get_connection()
            with conn:
                for msg_id in msg_ids:
                    if sess_id:
                        cur = conn.execute(
                            "SELECT entry_json FROM messages WHERE session_id = ? AND msg_id = ?",
                            (sess_id, msg_id)
                        )
                    else:
                        cur = conn.execute(
                            "SELECT entry_json FROM messages WHERE session_name = ? AND msg_id = ?",
                            (session_name_or_id, msg_id)
                        )
                    row = cur.fetchone()
                    if row:
                        try:
                            data = json.loads(row["entry_json"])
                            data["read"] = True
                            new_json = json.dumps(data)
                        except Exception:
                            new_json = row["entry_json"]
                        if sess_id:
                            conn.execute(
                                "UPDATE messages SET read = 1, entry_json = ? WHERE session_id = ? AND msg_id = ?",
                                (new_json, sess_id, msg_id)
                            )
                        else:
                            conn.execute(
                                "UPDATE messages SET read = 1, entry_json = ? WHERE session_name = ? AND msg_id = ?",
                                (new_json, session_name_or_id, msg_id)
                            )
                    else:
                        if sess_id:
                            conn.execute(
                                "UPDATE messages SET read = 1 WHERE session_id = ? AND msg_id = ?",
                                (sess_id, msg_id)
                            )
                        else:
                            conn.execute(
                                "UPDATE messages SET read = 1 WHERE session_name = ? AND msg_id = ?",
                                (session_name_or_id, msg_id)
                            )
        except Exception as e:
            logger.error(f"Failed to mark messages read for {session_name_or_id}: {e}")

    def load_session_messages(self, session_name_or_id: str) -> Dict[str, Dict[str, Any]]:
        if self._disabled:
            return {}
        messages: Dict[str, Dict[str, Any]] = {}
        try:
            sess_id = self.resolve_session_id(session_name_or_id)
            conn = self._get_connection()
            rows = []
            if sess_id:
                cur = conn.execute(
                    """
                    SELECT msg_id, read, entry_json
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY timestamp ASC
                    """,
                    (sess_id,)
                )
                rows = cur.fetchall()
            if not rows:
                cur = conn.execute(
                    """
                    SELECT msg_id, read, entry_json
                    FROM messages
                    WHERE session_name = ?
                    ORDER BY timestamp ASC
                    """,
                    (session_name_or_id,)
                )
                rows = cur.fetchall()

            for row in rows:
                try:
                    entry = json.loads(row["entry_json"])
                    entry["read"] = bool(row["read"])
                    messages[row["msg_id"]] = entry
                except Exception as e:
                    logger.warning(f"Failed to decode message {row['msg_id']}: {e}")
        except Exception as e:
            logger.error(f"Failed to load messages from store: {e}")
        return messages

    def rename_session(
        self,
        old_name: str,
        new_name: str,
        session_id: Optional[str] = None
    ) -> str:
        if self._disabled or self.read_only:
            raise RuntimeError(f"MessageStore is read-only or disabled: {self.last_error}")
        sess_id = session_id or self.resolve_session_id(old_name)
        if not sess_id:
            sess_id = self.ensure_session(old_name)

        conn = self._get_connection()
        now = time.time()
        with conn:
            cur = conn.execute("SELECT session_id FROM sessions WHERE current_name = ?", (new_name,))
            target = cur.fetchone()
            if target and target["session_id"] != sess_id:
                other_id = target["session_id"]
                conn.execute(
                    """
                    INSERT INTO messages (
                        msg_id, session_id, session_name, cwd_digest, direction, read,
                        sender, target_session, target_pid, content, status,
                        timestamp, entry_json
                    )
                    SELECT
                        msg_id, ?, ?, cwd_digest, direction, read,
                        sender, target_session, target_pid, content, status,
                        timestamp, entry_json
                    FROM messages
                    WHERE session_id = ?
                    ON CONFLICT(session_id, msg_id) DO NOTHING
                    """,
                    (sess_id, new_name, other_id)
                )
                conn.execute("DELETE FROM messages WHERE session_id = ?", (other_id,))
                conn.execute("UPDATE session_aliases SET session_id = ? WHERE session_id = ?", (sess_id, other_id))
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (other_id,))

            conn.execute(
                "INSERT OR REPLACE INTO session_aliases (alias_name, session_id, created_at) VALUES (?, ?, ?)",
                (old_name, sess_id, now)
            )
            conn.execute("DELETE FROM session_aliases WHERE alias_name = ?", (new_name,))
            conn.execute(
                "INSERT INTO session_aliases (alias_name, session_id, created_at) VALUES (?, ?, ?)",
                (new_name, sess_id, now)
            )
            conn.execute(
                "UPDATE sessions SET current_name = ?, updated_at = ? WHERE session_id = ?",
                (new_name, now, sess_id)
            )
            conn.execute(
                "UPDATE messages SET session_name = ? WHERE session_id = ?",
                (new_name, sess_id)
            )
        return sess_id

    def delete_message(self, session_name_or_id: str, msg_id: str):
        if self._disabled or self.read_only:
            return
        try:
            sess_id = self.resolve_session_id(session_name_or_id)
            conn = self._get_connection()
            with conn:
                if sess_id:
                    conn.execute("DELETE FROM messages WHERE session_id = ? AND msg_id = ?", (sess_id, msg_id))
                else:
                    conn.execute("DELETE FROM messages WHERE session_name = ? AND msg_id = ?", (session_name_or_id, msg_id))
        except Exception as e:
            logger.error(f"Failed to delete message {msg_id} for {session_name_or_id}: {e}")

    def clear_session_messages(self, session_name_or_id: str):
        if self._disabled or self.read_only:
            return
        try:
            sess_id = self.resolve_session_id(session_name_or_id)
            conn = self._get_connection()
            with conn:
                if sess_id:
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (sess_id,))
                else:
                    conn.execute("DELETE FROM messages WHERE session_name = ?", (session_name_or_id,))
        except Exception as e:
            logger.error(f"Failed to clear session messages for {session_name_or_id}: {e}")

    def prune(
        self,
        max_age_days: int = 30,
        max_messages_per_session: int = 1000,
        max_unread_age_days: int = 90
    ):
        if self._disabled or self.read_only:
            return
        try:
            conn = self._get_connection()
            now = time.time()
            cutoff = now - (max_age_days * 86400)
            unread_cutoff = now - (max_unread_age_days * 86400)
            with conn:
                conn.execute(
                    """
                    DELETE FROM messages
                    WHERE (read = 1 OR direction = 'outbound')
                      AND timestamp < ?
                    """,
                    (cutoff,)
                )
                conn.execute(
                    """
                    DELETE FROM messages
                    WHERE read = 0
                      AND direction = 'inbound'
                      AND timestamp < ?
                    """,
                    (unread_cutoff,)
                )
                cur = conn.execute("SELECT DISTINCT session_id FROM messages")
                sessions = [r[0] for r in cur.fetchall()]
                for sid in sessions:
                    conn.execute(
                        """
                        DELETE FROM messages
                        WHERE session_id = ?
                          AND (read = 1 OR direction = 'outbound')
                          AND msg_id NOT IN (
                              SELECT msg_id FROM messages
                              WHERE session_id = ?
                              ORDER BY timestamp DESC
                              LIMIT ?
                          )
                        """,
                        (sid, sid, max_messages_per_session)
                    )
            self._last_prune_time = now
        except Exception as e:
            logger.error(f"Error during message store pruning: {e}")

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class ResponseStore(dict):
    """
    In-memory dictionary that automatically persists additions and removals
    to MessageStore.
    """
    def __init__(
        self,
        store: Optional[MessageStore] = None,
        session_name: str = "",
        session_id: str = "",
        cwd_digest: str = "",
        initial: Optional[Dict[str, Dict[str, Any]]] = None
    ):
        super().__init__()
        self._store = store
        self._session_name = session_name
        self._session_id = session_id
        self._cwd_digest = cwd_digest
        if initial:
            super().update(initial)

    def set_session_name(self, session_name: str, session_id: Optional[str] = None):
        self._session_name = session_name
        if session_id:
            self._session_id = session_id

    def __setitem__(self, key: str, value: Dict[str, Any]):
        super().__setitem__(key, value)
        if self._store:
            self._store.save_message(
                key,
                value,
                self._session_name,
                self._cwd_digest,
                self._session_id
            )

    def __delitem__(self, key: str):
        super().__delitem__(key)
        if self._store:
            self._store.delete_message(self._session_id or self._session_name, key)

    def clear(self):
        super().clear()
        if self._store:
            self._store.clear_session_messages(self._session_id or self._session_name)

    def mark_read(self, msg_ids: List[str]):
        for mid in msg_ids:
            if mid in self and isinstance(self[mid], dict):
                self[mid]["read"] = True
        if self._store:
            self._store.mark_read(self._session_id or self._session_name, msg_ids)
