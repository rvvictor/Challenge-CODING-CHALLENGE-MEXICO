from __future__ import annotations

import json
import sqlite3
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
            self._sqlite = sqlite3.connect(db_path)
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
        try:
            rows = [(self.session_id, kind, json.dumps(payload, default=str), int(time.time() * 1000)) for payload in payloads]
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
