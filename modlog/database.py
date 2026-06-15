from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from modlog.audit_log import ModlogEvent


Order = Literal["asc", "desc"]


def discord_time_snowflake_offset(event_id: int, seconds: int, *, high: bool = False) -> int:
    import discord

    timestamp = discord.utils.snowflake_time(event_id) + timedelta(seconds=seconds)
    return discord.utils.time_snowflake(timestamp, high=high)


class ModlogEventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: int | None = None
    action: str | None = None
    actor_id: int | None = None
    target_id: int | None = None
    after_id: int | None = None
    before_id: int | None = None
    limit: int | None = Field(default=100, ge=1)
    offset: int = Field(default=0, ge=0)
    order: Order = "desc"


class ModlogDatabase:
    """SQLite storage for self-contained modlog event documents."""

    def __init__(self, path: str | Path = "modlog.sqlite3", *, initialize: bool = True) -> None:
        self.path = Path(path)
        if initialize:
            self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS modlog_events (
                    id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id INTEGER,
                    target_id INTEGER,
                    imported_at TEXT NOT NULL,
                    event_json TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_modlog_events_guild_id_id
                ON modlog_events(guild_id, id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_modlog_events_guild_action_id
                ON modlog_events(guild_id, action, id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_modlog_events_guild_actor_id
                ON modlog_events(guild_id, actor_id, id)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_modlog_events_guild_target_id
                ON modlog_events(guild_id, target_id, id)
            """)

    def write_event(self, event: ModlogEvent, *, replace: bool = False) -> bool:
        with self.connection() as connection:
            return self._write_event(connection, event, replace=replace)

    def write_events(self, events: Iterable[ModlogEvent], *, replace: bool = False) -> int:
        written = 0
        with self.connection() as connection:
            for event in events:
                if self._write_event(connection, event, replace=replace):
                    written += 1
        return written

    def read_event(self, event_id: int) -> ModlogEvent | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT event_json FROM modlog_events WHERE id = ?",
                (event_id,),
            ).fetchone()

        if row is None:
            return None
        return self._event_from_row(row)

    def query_events(
        self,
        *,
        guild_id: int | None = None,
        action: str | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        limit: int | None = 100,
        offset: int = 0,
        order: Order = "desc",
    ) -> list[ModlogEvent]:
        query = ModlogEventQuery(
            guild_id=guild_id,
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            after_id=after_id,
            before_id=before_id,
            limit=limit,
            offset=offset,
            order=order,
        )
        return list(self.iter_events(query))

    def related_events(
        self,
        event: ModlogEvent,
        *,
        seconds: int,
        actions: Iterable[str],
    ) -> list[ModlogEvent]:
        if event.target_id is None:
            return []
        after_id = discord_time_snowflake_offset(event.id, -seconds)
        before_id = discord_time_snowflake_offset(event.id, seconds, high=True)
        events: list[ModlogEvent] = []
        for action in actions:
            events.extend(self.query_events(
                guild_id=event.guild_id,
                action=action,
                target_id=event.target_id,
                after_id=after_id,
                before_id=before_id,
                limit=None,
            ))
        return [
            related
            for related in {related.id: related for related in events}.values()
            if related.id != event.id
        ]

    def write_event_with_links(
        self,
        event: ModlogEvent,
        *,
        related: Iterable[ModlogEvent],
        replace: bool = True,
    ) -> bool:
        related_events = list(related)
        event.related_event_ids = sorted({
            *event.related_event_ids,
            *(related_event.id for related_event in related_events),
        })
        with self.connection() as connection:
            written = self._write_event(connection, event, replace=replace)
            for related_event in related_events:
                related_event.related_event_ids = sorted({
                    *related_event.related_event_ids,
                    event.id,
                })
                self._write_event(connection, related_event, replace=True)
        return written

    def query_event_ids(
        self,
        *,
        guild_id: int | None = None,
        action: str | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        limit: int | None = None,
        order: Order = "desc",
    ) -> set[int]:
        query = ModlogEventQuery(
            guild_id=guild_id,
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            after_id=after_id,
            before_id=before_id,
            limit=limit,
            order=order,
        )
        where, params = self._where_clause(query)
        sql = f"SELECT id FROM modlog_events{where} ORDER BY id {'ASC' if order == 'asc' else 'DESC'}"
        if query.limit is not None:
            sql += " LIMIT ?"
            params.append(query.limit)
        if query.offset:
            if query.limit is None:
                sql += " LIMIT -1"
            sql += " OFFSET ?"
            params.append(query.offset)

        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()

        return {int(row["id"]) for row in rows}

    def iter_events(self, query: ModlogEventQuery | None = None) -> Iterator[ModlogEvent]:
        query = query or ModlogEventQuery()
        where, params = self._where_clause(query)
        order = "ASC" if query.order == "asc" else "DESC"
        sql = f"SELECT event_json FROM modlog_events{where} ORDER BY id {order}"
        if query.limit is not None:
            sql += " LIMIT ?"
            params.append(query.limit)
        if query.offset:
            if query.limit is None:
                sql += " LIMIT -1"
            sql += " OFFSET ?"
            params.append(query.offset)

        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()

        for row in rows:
            yield self._event_from_row(row)

    def count_events(self, *, guild_id: int | None = None) -> int:
        where = ""
        params: list[int] = []
        if guild_id is not None:
            where = " WHERE guild_id = ?"
            params.append(guild_id)

        with self.connection() as connection:
            value = connection.execute(
                f"SELECT COUNT(*) FROM modlog_events{where}",
                params,
            ).fetchone()[0]
        return int(value)

    def min_event_id(self, *, guild_id: int | None = None) -> int | None:
        return self._event_id_bound("MIN", guild_id=guild_id)

    def max_event_id(self, *, guild_id: int | None = None) -> int | None:
        return self._event_id_bound("MAX", guild_id=guild_id)

    def _write_event(
        self,
        connection: sqlite3.Connection,
        event: ModlogEvent,
        *,
        replace: bool,
    ) -> bool:
        if replace:
            existing = self._read_event(connection, event.id)
            if existing is not None:
                event.related_event_ids = sorted({
                    *existing.related_event_ids,
                    *event.related_event_ids,
                })
        values = (
            event.id,
            event.guild_id,
            event.action,
            event.actor_id,
            event.target_id,
            event.imported_at.isoformat(),
            event.to_json(),
        )
        if replace:
            cursor = connection.execute("""
                INSERT INTO modlog_events (
                    id,
                    guild_id,
                    action,
                    actor_id,
                    target_id,
                    imported_at,
                    event_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    action = excluded.action,
                    actor_id = excluded.actor_id,
                    target_id = excluded.target_id,
                    imported_at = excluded.imported_at,
                    event_json = excluded.event_json
            """, values)
        else:
            cursor = connection.execute("""
                INSERT OR IGNORE INTO modlog_events (
                    id,
                    guild_id,
                    action,
                    actor_id,
                    target_id,
                    imported_at,
                    event_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, values)
        return cursor.rowcount > 0

    def _read_event(self, connection: sqlite3.Connection, event_id: int) -> ModlogEvent | None:
        row = connection.execute(
            "SELECT event_json FROM modlog_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return self._event_from_row(row)

    def _where_clause(self, query: ModlogEventQuery) -> tuple[str, list[int | str]]:
        clauses: list[str] = []
        params: list[int | str] = []

        if query.guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(query.guild_id)
        if query.action is not None:
            clauses.append("action = ?")
            params.append(query.action)
        if query.actor_id is not None:
            clauses.append("actor_id = ?")
            params.append(query.actor_id)
        if query.target_id is not None:
            clauses.append("target_id = ?")
            params.append(query.target_id)
        if query.after_id is not None:
            clauses.append("id > ?")
            params.append(query.after_id)
        if query.before_id is not None:
            clauses.append("id < ?")
            params.append(query.before_id)

        if not clauses:
            return "", params
        return " WHERE " + " AND ".join(clauses), params

    def _event_id_bound(self, aggregate: Literal["MIN", "MAX"], *, guild_id: int | None) -> int | None:
        where = ""
        params: list[int] = []
        if guild_id is not None:
            where = " WHERE guild_id = ?"
            params.append(guild_id)

        with self.connection() as connection:
            value = connection.execute(
                f"SELECT {aggregate}(id) FROM modlog_events{where}",
                params,
            ).fetchone()[0]

        if value is None:
            return None
        return int(value)

    def _event_from_row(self, row: sqlite3.Row) -> ModlogEvent:
        raw_json = row["event_json"]
        if isinstance(raw_json, bytes):
            raw_json = raw_json.decode()
        if not isinstance(raw_json, str):
            raw_json = str(raw_json)
        return ModlogEvent.model_validate_json(raw_json)
