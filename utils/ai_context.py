from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from logging import Logger, getLogger
import re
import sqlite3
from typing import Any, Callable, Literal

import discord

SYSTEM_NAMESPACE = "system"
ASSISTANT_NAMESPACE = "assistant"
DEFAULT_HISTORY_PATH = "ai_history.sqlite3"
DEFAULT_HISTORY_CHAR_BUDGET = 10_000
ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")
USER_MENTION_RE = re.compile(r"<(@!?)([0-9]{15,20})>")
ROLE_MENTION_RE = re.compile(r"<@&([0-9]{15,20})>")
CHANNEL_MENTION_RE = re.compile(r"<#([0-9]{15,20})>")
_OPEN_TAG_NAMESPACE_RE = re.compile(rf"<\s*{re.escape(SYSTEM_NAMESPACE)}\s*:\s*", re.IGNORECASE)
_CLOSE_TAG_NAMESPACE_RE = re.compile(rf"<\s*/\s*{re.escape(SYSTEM_NAMESPACE)}\s*:\s*", re.IGNORECASE)

def system_tag(name: str) -> str:
    return f"{SYSTEM_NAMESPACE}:{name}"


def open_system_tag(name: str) -> str:
    return f"<{system_tag(name)}>"


def close_system_tag(name: str) -> str:
    return f"</{system_tag(name)}>"


def strip_context_tag_namespaces(text: str) -> str:
    text = _OPEN_TAG_NAMESPACE_RE.sub("<", text)
    return _CLOSE_TAG_NAMESPACE_RE.sub("</", text)


def strip_discord_reference_annotations(text: str) -> str:
    return ANNOTATED_DISCORD_REFERENCE_RE.sub(r"<\1\2>", text)


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ContextRequest:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    channel_id: int | None = None
    user_id: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    id: int | None = None


class AIContext:
    def __init__(
        self,
        *,
        normalize_discord: bool = True,
        history_enabled: bool = True,
        history_path: str = DEFAULT_HISTORY_PATH,
        history_char_budget: int = DEFAULT_HISTORY_CHAR_BUDGET,
        user_permission_level: Callable[[int], int] | None = None,
        logger: Logger | None = None,
    ):
        self.normalize_discord = normalize_discord
        self.history_enabled = history_enabled
        self.history_path = history_path
        self.history_char_budget = max(0, int(history_char_budget))
        self.user_permission_level = user_permission_level
        self.logger = logger or getLogger("Bogobot.AI.Context")

    def configure(
        self,
        *,
        normalize_discord: bool | None = None,
        history_enabled: bool | None = None,
        history_path: str | None = None,
        history_char_budget: int | None = None,
        user_permission_level: Callable[[int], int] | None = None,
        logger: Logger | None = None,
    ) -> None:
        if normalize_discord is not None:
            self.normalize_discord = normalize_discord
        if history_enabled is not None:
            self.history_enabled = history_enabled
        if history_path is not None:
            self.history_path = history_path
        if history_char_budget is not None:
            self.history_char_budget = max(0, int(history_char_budget))
        if user_permission_level is not None:
            self.user_permission_level = user_permission_level
        if logger is not None:
            self.logger = logger

    def format_message(
        self,
        content: str,
        source: discord.Message | discord.Interaction | None,
    ) -> str:
        if isinstance(source, discord.Message):
            content = self.annotate_discord_references(source, content)
            return self._format_message_content(
                content=content,
                user=source.author,
                message_id=source.id,
                interaction=False,
                interaction_data=source.interaction_metadata,
                created_at=source.created_at,
            )
        if isinstance(source, discord.Interaction):
            content = self.annotate_discord_references(source, content)
            return self._format_message_content(
                content=content,
                user=source.user,
                message_id=None,
                interaction=True,
                created_at=source.created_at,
            )
        return content.strip()

    def annotate_discord_references(
        self,
        source: discord.Message | discord.Interaction | None,
        text: str,
    ) -> str:
        if not self.normalize_discord or source is None:
            return text

        if isinstance(source, discord.Message):
            user_names = {
                str(user.id): self._discord_reference_name(user)
                for user in source.mentions
            }
            role_names = {
                str(role.id): self._discord_reference_name(role)
                for role in source.role_mentions
            }
            channel_names = {
                str(channel.id): self._discord_reference_name(channel)
                for channel in source.channel_mentions
            }
        else:
            user_names = {}
            role_names = {}
            channel_names = {}

        guild = source.guild
        if guild:
            for _, snowflake in USER_MENTION_RE.findall(text):
                if snowflake in user_names:
                    continue
                user = guild.get_member(int(snowflake))
                if user is not None:
                    user_names[snowflake] = self._discord_reference_name(user)
            for snowflake in ROLE_MENTION_RE.findall(text):
                if snowflake in role_names:
                    continue
                role = guild.get_role(int(snowflake))
                if role is not None:
                    role_names[snowflake] = self._discord_reference_name(role)
            for snowflake in CHANNEL_MENTION_RE.findall(text):
                if snowflake in channel_names:
                    continue
                channel = guild.get_channel(int(snowflake))
                if channel is not None:
                    channel_names[snowflake] = self._discord_reference_name(channel)

        def annotate_user(match: re.Match[str]) -> str:
            prefix, snowflake = match.groups()
            name = user_names.get(snowflake)
            if name is None:
                return match[0]
            return f"<{prefix}{snowflake} {json.dumps(name, ensure_ascii=False)}>"

        def annotate_role(match: re.Match[str]) -> str:
            snowflake = match[1]
            name = role_names.get(snowflake)
            if name is None:
                return match[0]
            return f"<@&{snowflake} {json.dumps(name, ensure_ascii=False)}>"

        def annotate_channel(match: re.Match[str]) -> str:
            snowflake = match[1]
            name = channel_names.get(snowflake)
            if name is None:
                return match[0]
            return f"<#{snowflake} {json.dumps(name, ensure_ascii=False)}>"

        text = USER_MENTION_RE.sub(annotate_user, text)
        text = ROLE_MENTION_RE.sub(annotate_role, text)
        return CHANNEL_MENTION_RE.sub(annotate_channel, text)

    def format_block(
        self,
        role: Literal["user", "assistant"],
        content: str,
    ) -> str:
        return content.strip()

    def format_command_call(self, command_name: str, arguments: dict[str, Any] | None = None) -> str:
        payload = {
            "name": command_name,
            "arguments": self._json_safe(arguments or {}),
        }
        return (
            f"{open_system_tag('command')}"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
            f"{close_system_tag('command')}"
        )

    def format_reply(self, content: str, source: discord.Message | discord.Interaction | None = None) -> str:
        return (
            f"{open_system_tag('replied_to')}\n"
            f"{self.format_message(content, source)}\n"
            f"{close_system_tag('replied_to')}"
        )

    def record_message(
        self,
        role: Literal["user", "assistant"],
        content: str,
        source: discord.Message | discord.Interaction | None = None,
        *,
        channel_id: int | None = None,
    ) -> None:
        channel_id = channel_id if channel_id is not None else self.source_channel_id(source)
        if not content.strip():
            return

        message = self.format_block(role, self.format_message(content, source))
        self.logger.debug(f"\n[role={role} channel_id={channel_id}]\n{message}")
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return

        self.record_history_message(channel_id, HistoryMessage(role, message))

    def record_reply(
        self,
        content: str,
        source: discord.Message | discord.Interaction | None = None,
        *,
        channel_id: int | None = None,
    ) -> None:
        channel_id = channel_id if channel_id is not None else self.source_channel_id(source)
        if not content.strip():
            return

        message = self.format_reply(content, source)
        self.logger.debug(f"\n[role=assistant channel_id={channel_id}]\n{message}")
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return

        self.record_history_message(channel_id, HistoryMessage("assistant", message))

    def history_messages(self, channel_id: int | None) -> list[HistoryMessage]:
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return []

        with closing(self._history_connection()) as connection:
            self._ensure_history_schema(connection)
            rows = connection.execute(
                """
                SELECT role, content
                FROM ai_history_messages
                WHERE channel_id = ?
                ORDER BY id
                """,
                (channel_id,),
            ).fetchall()
        return [
            HistoryMessage(role, content)
            for role, content in rows
        ]

    def record_history_message(
        self,
        channel_id: int,
        message: HistoryMessage,
    ) -> None:
        if not message.content:
            return

        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_history_schema(connection)
                connection.execute(
                    """
                    INSERT INTO ai_history_messages(channel_id, created_at, role, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (channel_id, datetime.now(timezone.utc).isoformat(), message.role, message.content),
                )
                self._evict_history(connection, channel_id)

    def queue_context_request(self, request: ContextRequest) -> ContextRequest:
        created_at = request.created_at or datetime.now(timezone.utc)
        expires_at = request.expires_at
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_context_request_schema(connection)
                cursor = connection.execute(
                    """
                    INSERT INTO ai_context_requests(
                        channel_id,
                        user_id,
                        created_at,
                        expires_at,
                        type,
                        payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.channel_id,
                        request.user_id,
                        created_at.astimezone(timezone.utc).isoformat(),
                        expires_at.astimezone(timezone.utc).isoformat() if expires_at is not None else None,
                        request.type,
                        json.dumps(self._json_safe(request.payload), ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an id for the queued AI context request.")
                request_id = int(cursor.lastrowid)
        return ContextRequest(
            id=request_id,
            type=request.type,
            payload=request.payload,
            channel_id=request.channel_id,
            user_id=request.user_id,
            created_at=created_at,
            expires_at=expires_at,
        )

    def query_context_requests(
        self,
        *,
        channel_id: int | None = None,
        user_id: int | None = None,
        include_expired: bool = False,
    ) -> list[ContextRequest]:
        conditions: list[str] = []
        params: list[Any] = []
        if channel_id is not None:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if not include_expired:
            conditions.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(datetime.now(timezone.utc).isoformat())

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with closing(self._history_connection()) as connection:
            self._ensure_context_request_schema(connection)
            rows = connection.execute(
                f"""
                SELECT id, channel_id, user_id, created_at, expires_at, type, payload
                FROM ai_context_requests
                {where_clause}
                ORDER BY id
                """,
                params,
            ).fetchall()
        return [self._context_request_from_row(row) for row in rows]

    def discard_context_request(self, request: ContextRequest) -> None:
        if request.id is None:
            return
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_context_request_schema(connection)
                connection.execute(
                    "DELETE FROM ai_context_requests WHERE id = ?",
                    (request.id,),
                )

    def source_channel_id(self, source: discord.Message | discord.Interaction | None) -> int | None:
        if isinstance(source, discord.Message):
            return source.channel.id
        if isinstance(source, discord.Interaction):
            return source.channel_id
        return None

    def _format_message_content(
        self,
        *,
        content: str,
        user: discord.User | discord.Member,
        message_id: int | None,
        interaction: bool,
        interaction_data: discord.MessageInteractionMetadata | None = None,
        created_at: datetime,
    ) -> str:
        id_line = f"id: {message_id}\n" if message_id is not None else ""
        interaction_line = "interaction: true\n" if interaction else ""
        interaction_text = f"from interaction: {self._format_interaction_metadata(interaction_data)}\n" if interaction_data is not None else ""
        timestamp = created_at.astimezone(timezone.utc).isoformat()
        perm_level = self._user_permission_level_name(user.id)
        content = content.strip()
        return (
            f"{open_system_tag('attached_metadata')}\n"
            f"{id_line}"
            f"{interaction_line}"
            f"{interaction_text}"
            f"time: {timestamp}\n"
            f"user: {user.id} {user.name} {json.dumps(user.display_name, ensure_ascii=False)}\n"
            f"perm_level: {perm_level}\n"
            f"{close_system_tag('attached_metadata')}\n"
            f"{content}"
        )

    def _user_permission_level(self, user_id: int) -> int:
        if self.user_permission_level is None:
            return 0
        try:
            return int(self.user_permission_level(user_id))
        except Exception:
            self.logger.exception(f"Could not resolve AI metadata permission level for user {user_id}.")
            return 0

    def _user_permission_level_name(self, user_id: int) -> str:
        from plugins.accounts import NAMES
        level = self._user_permission_level(user_id)
        return NAMES.get(level, f"level_{level}")
    
    def _format_interaction_metadata(self, meta: discord.MessageInteractionMetadata):
        all_data = {
            "id": str(meta.id),
            "type": meta.type.name,
            "user": {
                "id": str(meta.user.id),
                "name": str(meta.user),
                "display_name": meta.user.display_name
            },
            "target_user": {
                "id": str(meta.target_user.id),
                "name": str(meta.target_user),
                "display_name": meta.target_user.display_name
            } if meta.target_user is not None else None,
            "target_message_id": str(meta.target_message_id) if meta.target_message_id is not None else None,
        }
        return json.dumps(all_data)

    def _history_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.history_path)

    def _ensure_history_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ai_history_messages)").fetchall()
        }
        if columns and not {"id", "channel_id", "created_at"}.issubset(columns):
            connection.execute("DROP TABLE IF EXISTS ai_history_messages")
            connection.execute("DROP TABLE IF EXISTS ai_history_blocks")

        connection.execute("""
            CREATE TABLE IF NOT EXISTS ai_history_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_history_messages_channel_id_id
            ON ai_history_messages(channel_id, id)
        """)

    def _ensure_context_request_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS ai_context_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                type TEXT NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_context_requests_channel_user_id
            ON ai_context_requests(channel_id, user_id, id)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_context_requests_expires_at
            ON ai_context_requests(expires_at)
        """)

    def _context_request_from_row(self, row: sqlite3.Row | tuple[Any, ...]) -> ContextRequest:
        request_id, channel_id, user_id, created_at, expires_at, request_type, raw_payload = row
        payload: dict[str, Any]
        try:
            parsed_payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            parsed_payload = {}
        payload = parsed_payload if isinstance(parsed_payload, dict) else {}
        return ContextRequest(
            id=int(request_id),
            channel_id=int(channel_id) if channel_id is not None else None,
            user_id=int(user_id) if user_id is not None else None,
            created_at=datetime.fromisoformat(created_at),
            expires_at=datetime.fromisoformat(expires_at) if expires_at is not None else None,
            type=str(request_type),
            payload=payload,
        )

    def _evict_history(self, connection: sqlite3.Connection, channel_id: int) -> None:
        total = int(connection.execute(
            """
            SELECT COALESCE(SUM(LENGTH(content)), 0)
            FROM ai_history_messages
            WHERE channel_id = ?
            """,
            (channel_id,),
        ).fetchone()[0])
        while total > self.history_char_budget:
            row = connection.execute(
                """
                SELECT id, LENGTH(content)
                FROM ai_history_messages
                WHERE channel_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
            if row is None:
                return
            message_id, char_count = int(row[0]), int(row[1])
            connection.execute("DELETE FROM ai_history_messages WHERE id = ?", (message_id,))
            total -= char_count

    def _discord_reference_name(self, entity: 'discord.User | discord.Member | discord.Role | discord.abc.GuildChannel | discord.Thread') -> str:
        if isinstance(entity, discord.Member) or isinstance(entity, discord.User):
            return entity.display_name
        return entity.name

    def _json_safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (discord.User, discord.Member)):
            return {
                "id": value.id,
                "name": value.name,
                "display_name": value.display_name,
            }
        if isinstance(value, dict):
            return {
                str(key): self._json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._json_safe(item) for item in value]
        return str(value)
