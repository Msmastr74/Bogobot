from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from logging import Logger, getLogger
import re
import sqlite3
from typing import Any, Callable, Literal

import discord

SYSTEM_NAMESPACE = "|system|"
ASSISTANT_NAMESPACE = "|assistant|"
DEFAULT_HISTORY_PATH = "ai_history.sqlite3"
MAX_COMMANDS = 4
DEFAULT_HISTORY_CHAR_BUDGET = 10_000
ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")
USER_MENTION_RE = re.compile(r"<(@!?)([0-9]{15,20})>")
ROLE_MENTION_RE = re.compile(r"<@&([0-9]{15,20})>")
CHANNEL_MENTION_RE = re.compile(r"<#([0-9]{15,20})>")
@dataclass(frozen=True, slots=True)
class XMLTag:
    namespace: str | None
    name: str
    attrs: dict[str, str]
    body: str
    start: int
    end: int
    raw: str
    self_closing: bool


class XMLReader:
    def __init__(self, text: str):
        self.text = text

    def tags(self, namespace: str | None = None, name: str | None = None) -> list[XMLTag]:
        namespace = self._normalize_namespace(namespace)
        name = self._normalize_name(name) if name is not None else None
        tags: list[XMLTag] = []
        index = 0
        while index < len(self.text):
            start = self.text.find("<", index)
            if start < 0:
                break
            tag = self._read_open_tag(start)
            if tag is None:
                index = start + 1
                continue
            if (
                (namespace is None or tag.namespace == namespace)
                and (name is None or tag.name == name)
            ):
                tags.append(tag)
            index = max(tag.end, start + 1)
        return tags

    def remove(self, tags: list[XMLTag]) -> str:
        if not tags:
            return self.text
        parts: list[str] = []
        last_end = 0
        for tag in sorted(tags, key=lambda item: item.start):
            if tag.start < last_end:
                continue
            parts.append(self.text[last_end:tag.start])
            last_end = tag.end
        parts.append(self.text[last_end:])
        return "".join(parts)

    def rewrite(self, replacements: dict[int, str]) -> str:
        if not replacements:
            return self.text
        parts: list[str] = []
        last_end = 0
        for tag in self.tags():
            replacement = replacements.get(tag.start)
            if replacement is None or tag.start < last_end:
                continue
            parts.append(self.text[last_end:tag.start])
            parts.append(replacement)
            last_end = tag.end
        parts.append(self.text[last_end:])
        return "".join(parts)

    def strip_namespace(self, namespace: str) -> str:
        normalized_namespace = self._normalize_namespace(namespace)
        if normalized_namespace is None:
            return self.text
        output: list[str] = []
        index = 0
        while index < len(self.text):
            if self.text[index] != "<":
                output.append(self.text[index])
                index += 1
                continue
            replacement, end = self._strip_namespace_prefix_at(index, normalized_namespace)
            if replacement is None:
                output.append(self.text[index])
                index += 1
                continue
            output.append(replacement)
            index = end
        return "".join(output)

    def _strip_namespace_prefix_at(self, start: int, namespace: str) -> tuple[str | None, int]:
        index = start + 1
        closing = False
        if index < len(self.text) and self.text[index] == "/":
            closing = True
            index += 1
        stripped = False
        while True:
            attempt_start = index
            while index < len(self.text) and self.text[index].isspace():
                index += 1
            namespace_start = index
            while index < len(self.text) and self._is_name_char(self.text[index]):
                index += 1
            candidate = self.text[namespace_start:index].strip().casefold()
            if candidate != namespace:
                index = attempt_start
                break
            while index < len(self.text) and self.text[index].isspace():
                index += 1
            if index >= len(self.text) or self.text[index] != ":":
                index = attempt_start
                break
            stripped = True
            index += 1
            while index < len(self.text) and self.text[index].isspace():
                index += 1
        if not stripped:
            return None, start
        return ("</" if closing else "<"), index

    def _read_open_tag(self, start: int) -> XMLTag | None:
        open_end = self._find_tag_end(start)
        if open_end is None:
            return None
        inner = self.text[start + 1:open_end].strip()
        if not inner or inner.startswith(("/", "!", "?")):
            return None
        self_closing = inner.endswith("/")
        if self_closing:
            inner = inner[:-1].rstrip()
        full_name, attr_text = self._split_name_attrs(inner)
        if not full_name:
            return None
        namespace, name = self._split_namespace(full_name)
        body = ""
        end = open_end + 1
        if not self_closing:
            close_start, close_end = self._find_close_tag(full_name, end)
            if close_start is not None and close_end is not None:
                body = self.text[end:close_start]
                end = close_end
        return XMLTag(
            namespace=namespace,
            name=name,
            attrs=self._parse_attrs(attr_text),
            body=body,
            start=start,
            end=end,
            raw=self.text[start:end],
            self_closing=self_closing,
        )

    def _find_tag_end(self, start: int) -> int | None:
        quote: str | None = None
        index = start + 1
        while index < len(self.text):
            char = self.text[index]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == ">":
                return index
            index += 1
        return None

    def _find_close_tag(self, full_name: str, start: int) -> tuple[int | None, int | None]:
        index = start
        target_namespace, target_name = self._split_namespace(full_name)
        while index < len(self.text):
            close_start = self.text.find("</", index)
            if close_start < 0:
                return None, None
            close_end = self._find_tag_end(close_start)
            if close_end is None:
                return None, None
            close_inner = self.text[close_start + 2:close_end].strip()
            close_name, _attrs = self._split_name_attrs(close_inner)
            close_namespace, close_tag_name = self._split_namespace(close_name)
            if close_namespace == target_namespace and close_tag_name == target_name:
                return close_start, close_end + 1
            index = close_end + 1
        return None, None

    def _split_name_attrs(self, inner: str) -> tuple[str, str]:
        index = 0
        length = len(inner)
        while index < length and self._is_name_char(inner[index]):
            index += 1
        first = inner[:index].strip()
        while index < length and inner[index].isspace():
            index += 1
        if index < length and inner[index] == ":":
            index += 1
            while index < length and inner[index].isspace():
                index += 1
            name_start = index
            while index < length and self._is_name_char(inner[index]):
                index += 1
            second = inner[name_start:index].strip()
            while index < length and inner[index].isspace():
                index += 1
            return f"{first}:{second}", inner[index:].strip()
        return first, inner[index:].strip()

    def _split_namespace(self, full_name: str) -> tuple[str | None, str]:
        namespace, separator, name = full_name.partition(":")
        if not separator:
            return None, self._normalize_name(namespace)
        return self._normalize_namespace(namespace), self._normalize_name(name)

    def _normalize_namespace(self, namespace: str | None) -> str | None:
        if namespace is None:
            return None
        namespace = namespace.strip().casefold()
        return namespace or None

    def _normalize_name(self, name: str) -> str:
        return name.strip()

    def _is_name_char(self, char: str) -> bool:
        return char.isalnum() or char in "_-.|"

    def _parse_attrs(self, text: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            name_start = index
            while index < len(text) and not text[index].isspace() and text[index] not in "=/":
                index += 1
            attr_name = text[name_start:index]
            while index < len(text) and text[index].isspace():
                index += 1
            if not attr_name:
                index += 1
                continue
            if index >= len(text) or text[index] != "=":
                attrs[attr_name] = ""
                continue
            index += 1
            while index < len(text) and text[index].isspace():
                index += 1
            if index < len(text) and text[index] in ("'", '"'):
                quote = text[index]
                index += 1
                value_start = index
                while index < len(text) and text[index] != quote:
                    index += 1
                attrs[attr_name] = text[value_start:index]
                if index < len(text):
                    index += 1
            else:
                value_start = index
                while index < len(text) and not text[index].isspace():
                    index += 1
                attrs[attr_name] = text[value_start:index]
        return attrs

    def _format_attrs(self, attrs: dict[str, str]) -> str:
        if not attrs:
            return ""
        return "".join(
            f" {name}={json.dumps(value, ensure_ascii=False)}"
            for name, value in attrs.items()
        )

def system_tag(name: str) -> str:
    return f"{SYSTEM_NAMESPACE}:{name}"


def open_system_tag(name: str) -> str:
    return f"<{system_tag(name)}>"


def close_system_tag(name: str) -> str:
    return f"</{system_tag(name)}>"


def strip_context_tag_namespaces(text: str) -> str:
    return XMLReader(text).strip_namespace(SYSTEM_NAMESPACE)


def strip_discord_reference_annotations(text: str) -> str:
    return ANNOTATED_DISCORD_REFERENCE_RE.sub(r"<\1\2>", text)


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: Literal["user", "assistant"]
    content: str
    id: int | None = None
    channel_id: int | None = None
    created_at: datetime | None = None
    history_type: Literal["message", "event"] = "message"
    event_type: str | None = None


@dataclass(frozen=True, slots=True)
class ContextRequest:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    channel_id: int | None = None
    user_id: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class PersistentMemory:
    content: str
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AIContext:
    def __init__(
        self,
        *,
        normalize_discord: bool = True,
        history_enabled: bool = True,
        history_path: str = DEFAULT_HISTORY_PATH,
        history_char_budget: int = DEFAULT_HISTORY_CHAR_BUDGET,
        user_capabilities: Callable[[int], dict[str, int]] | None = None,
        logger: Logger | None = None,
    ):
        self.normalize_discord = normalize_discord
        self.history_enabled = history_enabled
        self.history_path = history_path
        self.history_char_budget = max(0, int(history_char_budget))
        self.user_capabilities = user_capabilities
        self.logger = logger or getLogger("Bogobot.AI.Context")

    def configure(
        self,
        *,
        normalize_discord: bool | None = None,
        history_enabled: bool | None = None,
        history_path: str | None = None,
        history_char_budget: int | None = None,
        user_capabilities: Callable[[int], dict[str, int]] | None = None,
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
        if user_capabilities is not None:
            self.user_capabilities = user_capabilities
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

    def format_tool_use_event(
        self,
        command_name: str,
        arguments: dict[str, Any] | None = None,
        source: discord.Message | discord.Interaction | None = None,
    ) -> str:
        payload = {
            "name": command_name,
            "arguments": self._json_safe(arguments or {}),
        }
        metadata = self.format_output_message_metadata(source)
        return "\n".join(
            part
            for part in (
                json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                metadata,
            )
            if part
        )

    def format_output_message_metadata(
        self,
        source: discord.Message | discord.Interaction | None,
    ) -> str:
        return self._format_source_metadata(source, tag_name="output_message_metadata")

    def format_attached_metadata(
        self,
        source: discord.Message | discord.Interaction | None,
    ) -> str:
        return self._format_source_metadata(source, tag_name="attached_metadata")

    def _format_source_metadata(
        self,
        source: discord.Message | discord.Interaction | None,
        *,
        tag_name: str,
    ) -> str:
        if isinstance(source, discord.Message):
            return self._format_attached_metadata(
                user=source.author,
                message_id=source.id,
                interaction=False,
                interaction_data=source.interaction_metadata,
                created_at=source.created_at,
                tag_name=tag_name,
            )
        if isinstance(source, discord.Interaction):
            return self._format_attached_metadata(
                user=source.user,
                message_id=None,
                interaction=True,
                created_at=source.created_at,
                tag_name=tag_name,
            )
        return ""

    def record_tool_use(
        self,
        command_name: str,
        arguments: dict[str, Any] | None = None,
        source: discord.Message | discord.Interaction | None = None,
        *,
        channel_id: int | None = None,
    ) -> None:
        channel_id = channel_id if channel_id is not None else self.source_channel_id(source)
        content = self.format_tool_use_event(command_name, arguments, source)
        self.logger.debug(f"\n[role=assistant channel_id={channel_id} history_type=event event_type=tool_use]\n{content}")
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return
        self.record_history_message(
            channel_id,
            HistoryMessage(
                "assistant",
                content,
                history_type="event",
                event_type="tool_use",
            ),
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
                SELECT id, role, content, history_type, event_type
                FROM ai_history_messages
                WHERE channel_id = ?
                ORDER BY id
                """,
                (channel_id,),
            ).fetchall()
        return [
            HistoryMessage(
                role,
                content,
                int(row_id),
                history_type=self._history_type(history_type),
                event_type=str(event_type) if event_type is not None else None,
            )
            for row_id, role, content, history_type, event_type in rows
        ]

    def channel_history_messages(self, channel_id: int) -> list[HistoryMessage]:
        with closing(self._history_connection()) as connection:
            self._ensure_history_schema(connection)
            rows = connection.execute(
                """
                SELECT id, channel_id, created_at, role, content, history_type, event_type
                FROM ai_history_messages
                WHERE channel_id = ?
                ORDER BY id
                """,
                (channel_id,),
            ).fetchall()
        return [
            HistoryMessage(
                id=int(row_id),
                channel_id=int(row_channel_id),
                created_at=datetime.fromisoformat(created_at),
                role=role,
                content=str(content),
                history_type=self._history_type(history_type),
                event_type=str(event_type) if event_type is not None else None,
            )
            for row_id, row_channel_id, created_at, role, content, history_type, event_type in rows
        ]

    def edit_history_message(self, message_id: int, content: str) -> HistoryMessage | None:
        content = content.strip()
        if not content:
            return None
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_history_schema(connection)
                cursor = connection.execute(
                    """
                    UPDATE ai_history_messages
                    SET content = ?
                    WHERE id = ?
                    """,
                    (content, message_id),
                )
                if cursor.rowcount < 1:
                    return None
                row = connection.execute(
                    """
                    SELECT id, channel_id, created_at, role, content, history_type, event_type
                    FROM ai_history_messages
                    WHERE id = ?
                    """,
                    (message_id,),
                ).fetchone()
        if row is None:
            return None
        row_id, channel_id, created_at, role, row_content, history_type, event_type = row
        return HistoryMessage(
            id=int(row_id),
            channel_id=int(channel_id),
            created_at=datetime.fromisoformat(created_at),
            role=role,
            content=str(row_content),
            history_type=self._history_type(history_type),
            event_type=str(event_type) if event_type is not None else None,
        )

    def remove_history_message(self, message_id: int) -> bool:
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_history_schema(connection)
                cursor = connection.execute(
                    "DELETE FROM ai_history_messages WHERE id = ?",
                    (message_id,),
                )
        return cursor.rowcount > 0

    def record_history_message(
        self,
        channel_id: int,
        message: HistoryMessage,
    ) -> None:
        if not message.content:
            return

        self.create_history_message(
            channel_id,
            message.role,
            message.content,
            history_type=message.history_type,
            event_type=message.event_type,
        )
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_history_schema(connection)
                self._evict_history(connection, channel_id)

    def create_history_message(
        self,
        channel_id: int,
        role: Literal["user", "assistant"],
        content: str,
        *,
        history_type: Literal["message", "event"] = "message",
        event_type: str | None = None,
    ) -> HistoryMessage | None:
        content = content.strip()
        if not content:
            return None
        if history_type == "message":
            event_type = None
        created_at = datetime.now(timezone.utc)
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_history_schema(connection)
                cursor = connection.execute(
                    """
                    INSERT INTO ai_history_messages(channel_id, created_at, role, content, history_type, event_type)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (channel_id, created_at.isoformat(), role, content, history_type, event_type),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an id for the AI history message.")
                message_id = int(cursor.lastrowid)
        return HistoryMessage(
            id=message_id,
            channel_id=channel_id,
            created_at=created_at,
            role=role,
            content=content,
            history_type=history_type,
            event_type=event_type,
        )

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

    def persistent_memories(self) -> list[PersistentMemory]:
        with closing(self._history_connection()) as connection:
            self._ensure_persistent_memory_schema(connection)
            rows = connection.execute(
                """
                SELECT id, created_at, updated_at, content
                FROM ai_persistent_memories
                ORDER BY id
                """,
            ).fetchall()
        return [
            PersistentMemory(
                id=int(memory_id),
                created_at=datetime.fromisoformat(created_at),
                updated_at=datetime.fromisoformat(updated_at),
                content=str(content),
            )
            for memory_id, created_at, updated_at, content in rows
        ]

    def next_persistent_memory_id(self) -> int:
        with closing(self._history_connection()) as connection:
            self._ensure_persistent_memory_schema(connection)
            sequence_row = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?",
                ("ai_persistent_memories",),
            ).fetchone()
            if sequence_row is not None and sequence_row[0] is not None:
                return int(sequence_row[0]) + 1
            max_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM ai_persistent_memories"
            ).fetchone()
        return int(max_row[0]) if max_row is not None else 1

    def create_persistent_memory(self, content: str) -> PersistentMemory | None:
        content = content.strip()
        if not content:
            return None
        now = datetime.now(timezone.utc)
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_persistent_memory_schema(connection)
                cursor = connection.execute(
                    """
                    INSERT INTO ai_persistent_memories(created_at, updated_at, content)
                    VALUES (?, ?, ?)
                    """,
                    (now.isoformat(), now.isoformat(), content),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an id for the persistent AI memory.")
                memory_id = int(cursor.lastrowid)
        return PersistentMemory(
            id=memory_id,
            created_at=now,
            updated_at=now,
            content=content,
        )

    def edit_persistent_memory(self, memory_id: int, content: str) -> PersistentMemory | None:
        content = content.strip()
        if not content:
            return None
        now = datetime.now(timezone.utc)
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_persistent_memory_schema(connection)
                cursor = connection.execute(
                    """
                    UPDATE ai_persistent_memories
                    SET updated_at = ?, content = ?
                    WHERE id = ?
                    """,
                    (now.isoformat(), content, memory_id),
                )
                if cursor.rowcount < 1:
                    return None
        return PersistentMemory(id=memory_id, updated_at=now, content=content)

    def remove_persistent_memory(self, memory_id: int) -> bool:
        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_persistent_memory_schema(connection)
                cursor = connection.execute(
                    "DELETE FROM ai_persistent_memories WHERE id = ?",
                    (memory_id,),
                )
        return cursor.rowcount > 0

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
        content = content.strip()
        return (
            f"{self._format_attached_metadata(user=user, message_id=message_id, interaction=interaction, interaction_data=interaction_data, created_at=created_at)}\n"
            f"{content}"
        )

    def _format_attached_metadata(
        self,
        *,
        user: discord.User | discord.Member,
        message_id: int | None,
        interaction: bool,
        interaction_data: discord.MessageInteractionMetadata | None = None,
        created_at: datetime,
        tag_name: str = "attached_metadata",
    ) -> str:
        id_line = f"id: {message_id}\n" if message_id is not None else ""
        interaction_line = "interaction: true\n" if interaction else ""
        interaction_text = f"from interaction: {self._format_interaction_metadata(interaction_data)}\n" if interaction_data is not None else ""
        timestamp = created_at.astimezone(timezone.utc).isoformat()
        capabilities = self._user_capabilities_text(user.id)
        return (
            f"{open_system_tag(tag_name)}\n"
            f"{id_line}"
            f"{interaction_line}"
            f"{interaction_text}"
            f"time: {timestamp}\n"
            f"user: {user.id} {user.name} {json.dumps(user.display_name, ensure_ascii=False)}\n"
            f"capabilities: {capabilities}\n"
            f"{close_system_tag(tag_name)}"
        )

    def _user_capabilities(self, user_id: int) -> dict[str, int]:
        if self.user_capabilities is None:
            return {}
        try:
            return self.user_capabilities(user_id)
        except Exception:
            self.logger.exception(f"Could not resolve AI metadata capabilities for user {user_id}.")
            return {}

    def _user_capabilities_text(self, user_id: int) -> str:
        capabilities = self._user_capabilities(user_id)
        if not capabilities:
            return "none"
        return ", ".join(
            f"{capability}:{depth}"
            for capability, depth in sorted(capabilities.items())
        )
    
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
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ai_history_messages)").fetchall()
        }
        if "history_type" not in columns:
            connection.execute(
                "ALTER TABLE ai_history_messages ADD COLUMN history_type TEXT NOT NULL DEFAULT 'message'"
            )
        if "event_type" not in columns:
            connection.execute(
                "ALTER TABLE ai_history_messages ADD COLUMN event_type TEXT"
            )
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_history_messages_channel_id_id
            ON ai_history_messages(channel_id, id)
        """)

    def _history_type(self, value: object) -> Literal["message", "event"]:
        return "event" if value == "event" else "message"

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

    def _ensure_persistent_memory_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS ai_persistent_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content TEXT NOT NULL
            )
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
