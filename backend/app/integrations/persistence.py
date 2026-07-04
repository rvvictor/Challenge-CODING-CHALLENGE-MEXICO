from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from backend.app.core.config import Settings


class DurableEventSink:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.persistence_enabled
        self.session_id = uuid.uuid4().hex
        self.driver = "disabled"
        self.status = "disabled"
        self.error = ""
        self._sqlite: sqlite3.Connection | None = None
        self._pg = None
        # Serializes all DB access: reads may run in worker threads
        # (asyncio.to_thread), so the sqlite connection is opened with
        # check_same_thread=False and guarded by this lock.
        self._lock = threading.Lock()
        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        if self.settings.database_url:
            self._connect_postgres()
        else:
            self._connect_sqlite()

    def _connect_postgres(self) -> None:
        try:
            import psycopg

            self._pg = psycopg.connect(self.settings.database_url, autocommit=True)
            with self._pg.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS aurelion_events (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            self.driver = "postgres"
            self.status = "connected"
            self.error = ""
        except Exception as exc:  # pragma: no cover - depends on optional external DB
            self.driver = "postgres"
            self.status = "unavailable"
            self.error = str(exc)

    def _connect_sqlite(self) -> None:
        try:
            db_path = Path(self.settings.sqlite_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(db_path, check_same_thread=False)
            self._sqlite.execute("PRAGMA journal_mode=WAL")
            self._sqlite.execute(
                """
                CREATE TABLE IF NOT EXISTS aurelion_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            self._sqlite.commit()
            self.driver = "sqlite"
            self.status = "connected"
            self.error = ""
        except Exception as exc:
            self.driver = "sqlite"
            self.status = "unavailable"
            self.error = str(exc)

    def append(self, kind: str, payload: dict) -> None:
        self.append_many(kind, [payload])

    def append_many(self, kind: str, payloads: list[dict]) -> None:
        if not self.enabled or self.status != "connected" or not payloads:
            return
        acquired = False
        try:
            rows = [(self.session_id, kind, json.dumps(payload, default=str), int(time.time() * 1000)) for payload in payloads]
            self._lock.acquire()
            acquired = True
            if self.driver == "postgres" and self._pg:
                with self._pg.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO aurelion_events (session_id, kind, payload) VALUES (%s, %s, %s::jsonb)",
                        [(session, item_kind, payload) for session, item_kind, payload, _created in rows],
                    )
                return
            if self.driver == "sqlite" and self._sqlite:
                self._sqlite.executemany(
                    "INSERT INTO aurelion_events (session_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    rows,
                )
                self._sqlite.commit()
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
        finally:
            if acquired:
                self._lock.release()

    def read(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        """Read recorded events for this session, newest first. Closes the loop on
        the 'durable + auditable' promise: replay/export can be served from the
        store rather than only from in-memory state."""
        if not self.enabled or self.status != "connected":
            return []
        limit = max(1, min(int(limit or 0), 2000))
        acquired = False
        try:
            self._lock.acquire()
            acquired = True
            if self.driver == "postgres" and self._pg:
                with self._pg.cursor() as cursor:
                    if kind:
                        cursor.execute(
                            "SELECT kind, payload, created_at FROM aurelion_events WHERE session_id = %s AND kind = %s ORDER BY id DESC LIMIT %s",
                            (self.session_id, kind, limit),
                        )
                    else:
                        cursor.execute(
                            "SELECT kind, payload, created_at FROM aurelion_events WHERE session_id = %s ORDER BY id DESC LIMIT %s",
                            (self.session_id, limit),
                        )
                    rows = cursor.fetchall()
                return [{"kind": row[0], "payload": row[1], "createdAt": row[2]} for row in rows]
            if self.driver == "sqlite" and self._sqlite:
                cursor = self._sqlite.cursor()
                if kind:
                    cursor.execute(
                        "SELECT kind, payload, created_at FROM aurelion_events WHERE session_id = ? AND kind = ? ORDER BY id DESC LIMIT ?",
                        (self.session_id, kind, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT kind, payload, created_at FROM aurelion_events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                        (self.session_id, limit),
                    )
                rows = cursor.fetchall()
                return [{"kind": row[0], "payload": json.loads(row[1]), "createdAt": row[2]} for row in rows]
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
        finally:
            if acquired:
                self._lock.release()
        return []

    def session_lineage(self, limit: int = 5) -> list[dict]:
        """Cross-session summaries from the durable store, newest first.

        Completes the auditable-session promise: a restart no longer hides the
        previous sessions — their trade counts and final P&L remain readable."""
        if not self.enabled or self.status != "connected":
            return []
        limit = max(1, min(int(limit or 0), 20))
        sessions: list[dict] = []
        acquired = False
        try:
            self._lock.acquire()
            acquired = True
            if self.driver == "sqlite" and self._sqlite:
                cursor = self._sqlite.cursor()
                cursor.execute(
                    "SELECT session_id, MIN(created_at), MAX(created_at), COUNT(*) FROM aurelion_events "
                    "GROUP BY session_id ORDER BY MAX(created_at) DESC LIMIT ?",
                    (limit,),
                )
                for session_id, started, ended, events in cursor.fetchall():
                    cursor.execute(
                        "SELECT COUNT(*) FROM aurelion_events WHERE session_id = ? AND kind = 'trade'",
                        (session_id,),
                    )
                    trades = int(cursor.fetchone()[0])
                    final_pnl = None
                    cursor.execute(
                        "SELECT payload FROM aurelion_events WHERE session_id = ? AND kind = 'trade' ORDER BY id DESC LIMIT 1",
                        (session_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        final_pnl = (json.loads(row[0]) or {}).get("cumulativePnl")
                    sessions.append({
                        "sessionId": session_id,
                        "current": session_id == self.session_id,
                        "startedAt": started,
                        "endedAt": ended,
                        "events": int(events),
                        "trades": trades,
                        "finalPnl": final_pnl,
                    })
            elif self.driver == "postgres" and self._pg:  # pragma: no cover - optional external DB
                with self._pg.cursor() as cursor:
                    cursor.execute(
                        "SELECT session_id, MIN(created_at), MAX(created_at), COUNT(*) FROM aurelion_events "
                        "GROUP BY session_id ORDER BY MAX(created_at) DESC LIMIT %s",
                        (limit,),
                    )
                    for session_id, started, ended, events in cursor.fetchall():
                        sessions.append({
                            "sessionId": session_id,
                            "current": session_id == self.session_id,
                            "startedAt": str(started),
                            "endedAt": str(ended),
                            "events": int(events),
                            "trades": None,
                            "finalPnl": None,
                        })
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
        finally:
            if acquired:
                self._lock.release()
        return sessions

    def count(self) -> int:
        if not self.enabled or self.status != "connected":
            return 0
        acquired = False
        try:
            self._lock.acquire()
            acquired = True
            if self.driver == "postgres" and self._pg:
                with self._pg.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM aurelion_events WHERE session_id = %s", (self.session_id,))
                    return int(cursor.fetchone()[0])
            if self.driver == "sqlite" and self._sqlite:
                cursor = self._sqlite.cursor()
                cursor.execute("SELECT COUNT(*) FROM aurelion_events WHERE session_id = ?", (self.session_id,))
                return int(cursor.fetchone()[0])
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
        finally:
            if acquired:
                self._lock.release()
        return 0

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "driver": self.driver,
            "status": self.status,
            "sessionId": self.session_id,
            "error": self.error,
            "postgresReady": self.driver == "postgres" and self.status == "connected",
        }

    def close(self) -> None:
        if self._sqlite:
            self._sqlite.close()
            self._sqlite = None
        if self._pg:
            self._pg.close()
            self._pg = None

    def __del__(self):  # pragma: no cover - defensive cleanup
        self.close()
