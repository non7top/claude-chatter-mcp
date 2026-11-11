"""
SQLite message persistence for claude-chatter: preserves inbound and outbound
messages across bridge restarts, tracking read/unread status.
"""
import json
import logging
import os
import sqlite3
import stat
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SocketBridgeMCP")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT NOT NULL,
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
    PRIMARY KEY (session_name, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_name, direction, read);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages (timestamp);
"""


class MessageStore:
    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.db_path = os.path.abspath(os.path.expanduser(db_path))
        else:
            env_path = os.environ.get("CHATTER_DB_PATH")
            if env_path:
                self.db_path = os.path.abspath(os.path.expanduser(env_path))
            else:
                self.db_path = os.path.expanduser("~/.claude/chatter-messages.db")

        self._conn: Optional[sqlite3.Connection] = None
        self._disabled = False
        self._init_db()

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
            with conn:
                conn.executescript(SCHEMA)
        except Exception as e:
            logger.error(f"Failed to initialize SQLite store at {self.db_path}: {e}")
            self._disabled = True

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._conn:
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA busy_timeout=5000;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    def save_message(
        self,
        msg_id: str,
        entry: Dict[str, Any],
        session_name: str,
        cwd_digest: str = ""
    ):
        if self._disabled:
            return
        try:
            conn = self._get_connection()
            direction = entry.get("direction", "inbound")
            read_val = 1 if entry.get("read", False) else 0
            sender = entry.get("sender")
            target_session = entry.get("target_session")
            target_pid = entry.get("target_pid")
            content = entry.get("content", "")
            status = entry.get("status", "")
            ts = float(entry.get("timestamp", time.time()))
            entry_json = json.dumps(entry)

            with conn:
                conn.execute(
                    """
                    INSERT INTO messages (
                        msg_id, session_name, cwd_digest, direction, read,
                        sender, target_session, target_pid, content, status,
                        timestamp, entry_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name, msg_id) DO UPDATE SET
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
                        msg_id, session_name, cwd_digest, direction, read_val,
                        sender, target_session, target_pid, content, status,
                        ts, entry_json
                    )
                )
        except Exception as e:
            logger.error(f"Failed to persist message {msg_id} for {session_name}: {e}")

    def mark_read(self, session_name: str, msg_ids: List[str]):
        if self._disabled or not msg_ids:
            return
        try:
            conn = self._get_connection()
            with conn:
                for msg_id in msg_ids:
                    cur = conn.execute(
                        "SELECT entry_json FROM messages WHERE session_name = ? AND msg_id = ?",
                        (session_name, msg_id)
                    )
                    row = cur.fetchone()
                    if row:
                        try:
                            data = json.loads(row["entry_json"])
                            data["read"] = True
                            new_json = json.dumps(data)
                        except Exception:
                            new_json = row["entry_json"]
                        conn.execute(
                            "UPDATE messages SET read = 1, entry_json = ? WHERE session_name = ? AND msg_id = ?",
                            (new_json, session_name, msg_id)
                        )
                    else:
                        conn.execute(
                            "UPDATE messages SET read = 1 WHERE session_name = ? AND msg_id = ?",
                            (session_name, msg_id)
                        )
        except Exception as e:
            logger.error(f"Failed to mark messages read for {session_name}: {e}")

    def load_session_messages(self, session_name: str) -> Dict[str, Dict[str, Any]]:
        if self._disabled:
            return {}
        messages: Dict[str, Dict[str, Any]] = {}
        try:
            conn = self._get_connection()
            cur = conn.execute(
                """
                SELECT msg_id, read, entry_json
                FROM messages
                WHERE session_name = ?
                ORDER BY timestamp ASC
                """,
                (session_name,)
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

    def rename_session(self, old_name: str, new_name: str):
        if self._disabled:
            return
        try:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    "UPDATE messages SET session_name = ? WHERE session_name = ?",
                    (new_name, old_name)
                )
        except Exception as e:
            logger.error(f"Failed to update session name in message store: {e}")

    def delete_message(self, session_name: str, msg_id: str):
        if self._disabled:
            return
        try:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    "DELETE FROM messages WHERE session_name = ? AND msg_id = ?",
                    (session_name, msg_id)
                )
        except Exception as e:
            logger.error(f"Failed to delete message {msg_id} for {session_name}: {e}")

    def clear_session_messages(self, session_name: str):
        if self._disabled:
            return
        try:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM messages WHERE session_name = ?", (session_name,))
        except Exception as e:
            logger.error(f"Failed to clear session messages for {session_name}: {e}")

    def prune(
        self,
        max_age_days: int = 30,
        max_messages_per_session: int = 1000,
        max_unread_age_days: int = 90
    ):
        if self._disabled:
            return
        try:
            conn = self._get_connection()
            cutoff = time.time() - (max_age_days * 86400)
            unread_cutoff = time.time() - (max_unread_age_days * 86400)
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
                cur = conn.execute("SELECT DISTINCT session_name FROM messages")
                sessions = [r[0] for r in cur.fetchall()]
                for sname in sessions:
                    conn.execute(
                        """
                        DELETE FROM messages
                        WHERE session_name = ?
                          AND (read = 1 OR direction = 'outbound')
                          AND msg_id NOT IN (
                              SELECT msg_id FROM messages
                              WHERE session_name = ?
                              ORDER BY timestamp DESC
                              LIMIT ?
                          )
                        """,
                        (sname, sname, max_messages_per_session)
                    )
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
        cwd_digest: str = "",
        initial: Optional[Dict[str, Dict[str, Any]]] = None
    ):
        super().__init__()
        self._store = store
        self._session_name = session_name
        self._cwd_digest = cwd_digest
        if initial:
            super().update(initial)

    def set_session_name(self, session_name: str):
        self._session_name = session_name

    def __setitem__(self, key: str, value: Dict[str, Any]):
        super().__setitem__(key, value)
        if self._store:
            self._store.save_message(key, value, self._session_name, self._cwd_digest)

    def __delitem__(self, key: str):
        super().__delitem__(key)
        if self._store:
            self._store.delete_message(self._session_name, key)

    def clear(self):
        super().clear()
        if self._store:
            self._store.clear_session_messages(self._session_name)

    def mark_read(self, msg_ids: List[str]):
        for mid in msg_ids:
            if mid in self and isinstance(self[mid], dict):
                self[mid]["read"] = True
        if self._store:
            self._store.mark_read(self._session_name, msg_ids)
