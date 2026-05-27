import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from logging import Logger, WARNING, getLogger
import os
import re
import sqlite3
import time
import types
from typing import Any, Callable, Generic, Literal, TypeAlias, TypeVar, Union, cast, get_args, get_origin, TYPE_CHECKING
if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionToolParam, ChatCompletionMessageToolCallUnion

import discord
import plugins.ai as ai_plugin

getLogger("httpx").setLevel(WARNING)

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")

AIParamsTable: TypeAlias = dict[str, "AIParam"]
_MAX_CALLS = 4
DEFAULT_REQUEST_INTERVAL_SECONDS = 60.0
DEFAULT_HISTORY_PATH = "ai_history.sqlite3"
DEFAULT_HISTORY_CHAR_BUDGET = 10_000
ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")
USER_MENTION_RE = re.compile(r"<(@!?)([0-9]{15,20})>")
ROLE_MENTION_RE = re.compile(r"<@&([0-9]{15,20})>")
CHANNEL_MENTION_RE = re.compile(r"<#([0-9]{15,20})>")
_THOUGHT_BLOCK_RE = re.compile(r"<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)
_CTX_OPEN_TAG_NAMESPACE_RE = re.compile(r"<\s*ctx\s*:\s*", re.IGNORECASE)
_CTX_CLOSE_TAG_NAMESPACE_RE = re.compile(r"<\s*/\s*ctx\s*:\s*", re.IGNORECASE)

@dataclass(frozen=True, slots=True)
class AIParam:
    description: str | None = None
    type: object = str
    required: bool = True
    default: Any = None


@dataclass(frozen=True, slots=True)
class AIMatch(Generic[ContextT, ActionT]):
    name: str
    command_name: str
    description: str
    context: ContextT
    action: ActionT | None
    score: float
    kwargs: dict[str, Any] | None = None
    reply: str | None = None


@dataclass(frozen=True, slots=True)
class _AIAction(Generic[ContextT, ActionT]):
    name: str
    command_name: str
    tool_name: str
    description: str
    params: dict[str, AIParam]
    context: ContextT
    action: ActionT


@dataclass(frozen=True, slots=True)
class _ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _HistoryMessage:
    role: Literal["user", "assistant"]
    content: str


class AICore(Generic[ContextT, ActionT]):
    def __init__(
        self,
        *,
        enabled: bool = True,
        model_name: str = "gpt-4o-mini",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        normalize_discord: bool = True,
        history_enabled: bool = True,
        history_path: str = DEFAULT_HISTORY_PATH,
        history_char_budget: int = DEFAULT_HISTORY_CHAR_BUDGET,
        logger: Logger | None = None,
    ):
        self.enabled = enabled
        self.model_name = model_name
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.request_interval_seconds = max(0.0, float(request_interval_seconds))
        self.normalize_discord = normalize_discord
        self.history_enabled = history_enabled
        self.history_path = history_path
        self.history_char_budget = max(0, int(history_char_budget))
        self._actions: list[_AIAction[ContextT, ActionT]] = []
        self._client: 'AsyncOpenAI | None' = None
        self._last_request_at: float | None = None
        self._lock = asyncio.Lock()
        self.logger = logger or getLogger("Bogobot.AI")

    def configure(
        self,
        *,
        enabled: bool | None = None,
        model_name: str | None = None,
        api_key_env: str | None = None,
        base_url: str | None = None,
        request_interval_seconds: float | None = None,
        normalize_discord: bool | None = None,
        history_enabled: bool | None = None,
        history_path: str | None = None,
        history_char_budget: int | None = None,
        logger: Logger | None = None,
        model: str | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = enabled

        chosen_model = model_name or model
        if chosen_model is not None and chosen_model != self.model_name:
            self.model_name = chosen_model

        if api_key_env is not None:
            self.api_key_env = api_key_env

        if base_url != self.base_url:
            self.base_url = base_url
            self._client = None

        if request_interval_seconds is not None:
            self.request_interval_seconds = max(0.0, float(request_interval_seconds))

        if normalize_discord is not None:
            self.normalize_discord = normalize_discord

        if history_enabled is not None:
            self.history_enabled = history_enabled

        if history_path is not None:
            self.history_path = history_path

        if history_char_budget is not None:
            self.history_char_budget = max(0, int(history_char_budget))

        if logger is not None:
            self.logger = logger

    def action(
        self,
        name: str,
        description: str,
        command_name: str | None = None,
        params: AIParamsTable | None = None,
        **kwargs: Any,
    ) -> Callable[[ActionT], ActionT]:
        def decorator(action: ActionT) -> ActionT:
            normalized_params = self._normalize_params(params or {})
            self._actions.append(_AIAction(
                name=name,
                command_name=command_name or name,
                tool_name=self._unique_tool_name(name),
                description=description,
                params=normalized_params,
                context=cast(ContextT, kwargs),
                action=action,
            ))
            return action
        return decorator

    def format_message(
        self,
        content: str,
        source: discord.Message | discord.Interaction | None,
    ) -> str:
        content = self.strip_context_tag_namespaces(content)
        if isinstance(source, discord.Message):
            content = self.annotate_discord_references(source, content)
            return self._format_message_content(
                content=content,
                user=source.author,
                message_id=source.id,
                interaction=False,
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
            user_names: dict[str, str] = {}
            role_names: dict[str, str] = {}
            channel_names: dict[str, str] = {}
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
            "<ctx:command>"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
            "</ctx:command>"
        )

    def format_reply(self, content: str, source: discord.Message | discord.Interaction | None = None) -> str:
        return (
            "<ctx:replied_to>\n"
            f"{self.format_message(content, source)}\n"
            "</ctx:replied_to>"
        )

    def _format_message_content(
        self,
        *,
        content: str,
        user: discord.User | discord.Member,
        message_id: int | None,
        interaction: bool,
        created_at: datetime,
    ) -> str:
        id_line = f"id: {message_id}\n" if message_id is not None else ""
        interaction_line = "interaction: true\n" if interaction else ""
        timestamp = created_at.astimezone(timezone.utc).isoformat()
        content = content.strip()
        return (
            "<ctx:attached_metadata>\n"
            f"{id_line}"
            f"{interaction_line}"
            f"time: {timestamp}\n"
            f"user: {user.id} {user.name} {json.dumps(user.display_name, ensure_ascii=False)}\n"
            "</ctx:attached_metadata>\n"
            f"{content}"
        )

    def record_message(
        self,
        role: Literal["user", "assistant"],
        content: str,
        source: discord.Message | discord.Interaction | None = None,
        *,
        channel_id: int | None = None,
    ) -> None:
        channel_id = channel_id if channel_id is not None else self._source_channel_id(source)
        if not content.strip():
            return

        message = self.format_block(role, self.format_message(content, source))
        self.logger.debug(f"\n[role={role} channel_id={channel_id}]\n{message}")
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return

        self._record_history_message(
            channel_id,
            _HistoryMessage(role, message),
        )

    def record_reply(
        self,
        content: str,
        source: discord.Message | discord.Interaction | None = None,
        *,
        channel_id: int | None = None,
    ) -> None:
        channel_id = channel_id if channel_id is not None else self._source_channel_id(source)
        if not content.strip():
            return

        message = self.format_reply(content, source)
        self.logger.debug(f"\n[role=assistant channel_id={channel_id}]\n{message}")
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return

        self._record_history_message(
            channel_id,
            _HistoryMessage("assistant", message),
        )

    async def match(self, text: str) -> ActionT | None:
        matches = await self.ai_turn(text)
        if not matches:
            return None
        return matches[0].action

    async def ai_turn(
        self,
        text: str,
        *,
        source: discord.Message | discord.Interaction | None = None,
        assistant_context: str | None = None,
        assistant_context_source: discord.Message | discord.Interaction | None = None,
    ) -> list[AIMatch[ContextT, ActionT]]:
        if not self.enabled or not text.strip():
            return []

        channel_id = self._source_channel_id(source)
        history = self._history_messages(channel_id)
        if assistant_context is not None:
            self.record_reply(
                assistant_context,
                assistant_context_source,
                channel_id=channel_id,
            )
        self.record_message("user", text, source, channel_id=channel_id)
        formatted_text = self.format_block(
            "user",
            self.format_message(text, source),
        )
        formatted_assistant_context = (
            self.format_reply(assistant_context, assistant_context_source)
            if assistant_context is not None else
            None
        )

        async with self._lock:
            client = self._ensure_client()
            await self._wait_for_rate_limit()
            content, calls = await self._complete(
                client,
                formatted_text,
                self._actions,
                formatted_assistant_context,
                history,
            )

        matches: list[AIMatch[ContextT, ActionT]] = []
        if not calls:
            reply = self._coerce_reply(content)
            if reply is None:
                return []
            return [AIMatch(
                name="conversation",
                command_name="conversation",
                description="Conversational AI response",
                context=cast(ContextT, {}),
                action=None,
                score=1.0,
                reply=reply,
            )]

        action_by_tool = {action.tool_name: action for action in self._actions}
        message_source = source if isinstance(source, discord.Message) else None
        interaction_source = source if isinstance(source, discord.Interaction) else None
        for call in calls[:_MAX_CALLS]:
            action = action_by_tool.get(call.name)
            if action is None:
                self.logger.debug(f"tool call rejected unknown action {call.name!r}.")
                continue

            kwargs = self._coerce_arguments(
                action,
                call.arguments,
                message=message_source,
                interaction=interaction_source,
            )
            if kwargs is None:
                self.logger.debug(f"tool call {call.name} rejected because arguments did not validate: {call.arguments!r}.")
                continue

            self.logger.debug(f"match succeeded for {text} with action {action.name}.")
            matches.append(AIMatch(
                name=action.name,
                command_name=action.command_name,
                description=action.description,
                context=action.context,
                action=action.action,
                score=1.0,
                kwargs=kwargs,
            ))

        return matches

    def _source_channel_id(self, source: discord.Message | discord.Interaction | None) -> int | None:
        if isinstance(source, discord.Message):
            return source.channel.id
        if isinstance(source, discord.Interaction):
            return source.channel_id
        return None

    def _record_history_message(
        self,
        channel_id: int,
        message: _HistoryMessage,
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

    def _history_messages(self, channel_id: int | None) -> list[_HistoryMessage]:
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
            _HistoryMessage(role, content)
            for role, content in rows
        ]

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

    async def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            wait_seconds = self.request_interval_seconds - (now - self._last_request_at)
            if wait_seconds > 0:
                self.logger.debug(f"rate limit sleeping for {wait_seconds:.2f}s.")
                await asyncio.sleep(wait_seconds)
                now = time.monotonic()
        self._last_request_at = now

    def _ensure_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError("The openai package is required when AI is enabled.") from exc

            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise RuntimeError(f"AI is enabled but {self.api_key_env} is not set.")
            self._client = AsyncOpenAI(api_key=api_key, base_url=self.base_url)
        return self._client

    async def _complete(
        self,
        client: 'AsyncOpenAI',
        text: str,
        actions: list[_AIAction[ContextT, ActionT]],
        reply_message: str | None,
        history: list[_HistoryMessage],
    ) -> tuple[str, list[_ToolCall]]:
        reply_message_text = reply_message.strip() if reply_message is not None else ""
        has_reply_message = bool(reply_message_text)
        system_prompt = self._system_prompt(actions)
        tools = [self._tool_schema(action) for action in actions]
        messages: list[Any] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(
            {"role": item.role, "content": item.content}
            for item in history
        )
        if has_reply_message:
            messages.append({"role": "assistant", "content": reply_message_text})
        messages.append({"role": "user", "content": text})
        try:
            response = await client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=512,
            )
        except Exception as exc:
            tool_error = self._tool_use_failed_message(exc)
            if tool_error is None:
                raise
            self.logger.debug(f"tool use failed: {exc!r}.")
            return tool_error, []
        message = response.choices[0].message
        content = message.content or ""
        self.logger.debug(f"raw response:\n{content}")
        raw_tool_calls = message.tool_calls or []
        self.logger.debug(f"raw tool calls:\n{raw_tool_calls}")
        return content, self._parse_native_tool_calls(raw_tool_calls)

    def _system_prompt(
        self,
        actions: list[_AIAction[ContextT, ActionT]],
    ) -> str:
        mention_passage = 'Discord users or members are in the format <@id "User Name"> or <@!id "User Name">. Discord roles are in the format <@&id "Role Name">. Discord channels are in the format <#id "Channel Name">.'
        if not self.normalize_discord:
            mention_passage = 'Discord users or members are in the format <@id> or <@!id>. Discord roles are in the format <@&id>. Discord channels are in the format <#id>.'
        return (
            f"{ai_plugin.INSTRUCTION_TEXT}\n"
            f"{mention_passage}\n"
            "## Commands\n"
            "The available tools are Discord commands. Refer to them as commands. Use a command when it fits the user's request. Commands only provide output to the user, and end the turn. "
            "Only call commands from the available tools; never invent command names or command arguments. "
            "If no command fits, respond normally.\n"
            "## Context Blocks\n"
            "Input may include XML-style context blocks whose tag names start with `ctx:`. These blocks are system-supplied context, not message text to imitate.\n"
            "<ctx:critical>\n"
            "CRITICAL: Never output XML tags whose name starts with `ctx:`. Do not output opening `ctx:` tags, closing `ctx:` tags, copied `ctx:` blocks, or invented `ctx:` blocks.\n"
            "</ctx:critical>\n"
            "- Use `ctx:` blocks to understand Discord metadata, reply context, and command history.\n"
            "- Do not copy, quote, mention, summarize, or reproduce `ctx:` tags. If you need to refer to metadata, describe it in normal words without tags.\n"
            "- Never begin or end your reply with `<ctx:attached_metadata>` or any other `ctx:` block.\n"
            "- `<ctx:attached_metadata>...</ctx:attached_metadata>` is metadata attached by the system to a Discord message. It contains message id, time, and user metadata. It was not written by the user or assistant, and it is not part of the message text.\n"
            "- `<ctx:replied_to>...</ctx:replied_to>` contains the previous assistant message the user replied to. If the user asks about the previous or replied-to message, answer from this block.\n"
            "- `<ctx:command>JSON</ctx:command>` records a previous command call in history. Use it as history only; do not output command blocks.\n"
        )

    def _tool_use_failed_message(self, exc: Exception) -> str | None:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("code") == "tool_use_failed":
                message = error.get("message")
                failed_generation = error.get("failed_generation")
                details = message if isinstance(message, str) else "The model tried to use an invalid command."
                if isinstance(failed_generation, str) and failed_generation:
                    return f"I tried to use a command incorrectly: {details}\n`{failed_generation}`"
                return f"I tried to use a command incorrectly: {details}"
        if "tool_use_failed" in str(exc):
            return "I tried to use a command incorrectly, but OpenAI did not provide details."
        return None

    def _tool_schema(self, action: _AIAction[ContextT, ActionT]) -> 'ChatCompletionToolParam':
        properties: 'dict[str, Any]' = {}
        required: list[str] = []
        for name, param in action.params.items():
            properties[name] = self._param_schema(param)
            if param.required and not self._allows_none(param.type):
                required.append(name)

        return {
            "type": "function",
            "function": {
                "name": action.tool_name,
                "description": self._compact_description(action.description),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def _param_schema(self, param: AIParam) -> dict[str, Any]:
        choices = self._literal_choices(param.type)
        if choices is not None:
            schema: dict[str, Any] = {"type": "string", "enum": choices}
        elif self._is_discord_user_type(param.type):
            schema = {
                "type": "string",
                "description": "Discord user id.",
            }
        else:
            schema = {"type": self._json_schema_type(param.type)}
        if self._allows_none(param.type):
            raw_type = schema.get("type")
            if isinstance(raw_type, str):
                schema["type"] = [raw_type, "null"]
            if "enum" in schema:
                schema["enum"] = [*schema["enum"], None]
        if param.description is not None and "description" not in schema:
            schema["description"] = self._compact_description(param.description)
        if not param.required:
            schema["default"] = param.default
        return schema

    def _json_schema_type(self, annotation: object) -> str:
        target = self._non_none_type(annotation)
        if target is int:
            return "integer"
        if target is float:
            return "number"
        if target is bool:
            return "boolean"
        return "string"

    def _parse_native_tool_calls(self, raw_tool_calls: list['ChatCompletionMessageToolCallUnion']) -> list[_ToolCall]:
        calls: list[_ToolCall] = []
        for raw_call in raw_tool_calls:
            if raw_call.type != 'function':
                continue
            function = raw_call.function
            name = function.name
            raw_arguments = function.arguments
            if not isinstance(name, str):
                continue
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                self.logger.debug(f"tool call {name} rejected invalid JSON arguments: {raw_arguments!r}.")
                continue
            if arguments is None:
                arguments = {}
            if isinstance(arguments, dict):
                calls.append(_ToolCall(name, arguments))
        return calls

    def _coerce_reply(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        if self._should_strip_first_thought_block():
            value = _THOUGHT_BLOCK_RE.sub("", value, count=1)
        value = self.strip_context_tag_namespaces(value)
        value = value.strip()
        return value if self._discord_string_valid(value) else None

    def visual_reply(self, value: str) -> str | None:
        value = self.strip_context_tag_namespaces(value)
        value = self.strip_discord_reference_annotations(value)
        value = value.strip()
        return value if self._discord_string_valid(value) else None

    def _should_strip_first_thought_block(self) -> bool:
        model = self.model_name.casefold()
        if "gemini" not in model and "gemma" not in model:
            return False
        base_url = (self.base_url or "").casefold()
        return "google" in base_url or "generativelanguage.googleapis.com" in base_url

    def _discord_string_valid(self, value: str) -> bool:
        return bool(value) and not value[0].isspace() and not value[-1].isspace()

    def _coerce_arguments(
        self,
        action: _AIAction[ContextT, ActionT],
        raw_args: dict[str, Any],
        *,
        message: discord.Message | None,
        interaction: discord.Interaction | None,
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {}
        allowed = set(action.params)
        if any(name not in allowed for name in raw_args):
            return None

        for name, param in action.params.items():
            raw_value = raw_args.get(name, _MISSING)
            value = self._coerce_value(
                param.type,
                raw_value,
                message=message,
                interaction=interaction,
            )
            if value is _MISSING:
                if not param.required:
                    continue
                if self._allows_none(param.type):
                    kwargs[name] = None
                    continue
                return None
            kwargs[name] = value
        return kwargs

    def _coerce_value(
        self,
        annotation: object,
        value: Any,
        *,
        message: discord.Message | None,
        interaction: discord.Interaction | None,
    ) -> Any:
        target = self._non_none_type(annotation)
        if value is _MISSING or value is None:
            return _MISSING
        if self._is_discord_user_type(target):
            return self._coerce_discord_user(
                target,
                value,
                message=message,
                interaction=interaction,
            )

        choices = self._literal_choices(annotation)
        if choices is not None:
            return value if isinstance(value, str) and value in choices else _MISSING

        if target is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, str) and re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", value.strip()):
                return int(value.replace(",", ""))
            return _MISSING
        if target is float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.strip().replace(",", ""))
                except ValueError:
                    return _MISSING
            return _MISSING
        if target is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.casefold() in ("true", "false"):
                return value.casefold() == "true"
            return _MISSING
        if target in (str, object):
            string = self.strip_discord_reference_annotations(str(value))
            string = self.strip_context_tag_namespaces(string)
            string = string.strip()
            return string if self._discord_string_valid(string) else _MISSING
        return value

    def _coerce_discord_user(
        self,
        annotation: object,
        value: Any,
        *,
        message: discord.Message | None,
        interaction: discord.Interaction | None,
    ) -> Any:
        if isinstance(value, (discord.User, discord.Member)):
            return value

        user_id = self._discord_user_id(value)
        if user_id is None:
            return _MISSING

        guild = message.guild if message is not None else interaction.guild if interaction is not None else None
        if guild is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member

        if message is not None:
            for user in message.mentions:
                if user.id == user_id:
                    return user

        if interaction is not None and interaction.user.id == user_id:
            return interaction.user

        source = message if message is not None else interaction
        state = getattr(source, "_state", None)
        cached_user = getattr(state, "get_user", lambda _user_id: None)(user_id)
        if cached_user is not None and annotation is not discord.Member:
            return cached_user
        return _MISSING

    def _discord_user_id(self, value: Any) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        value = self.strip_discord_reference_annotations(value.strip())
        match = re.fullmatch(r"<@!?([0-9]{15,20})(?: .*)?>", value)
        if match is not None:
            return int(match[1])
        if re.fullmatch(r"[0-9]{15,20}", value):
            return int(value)
        return None

    def strip_discord_reference_annotations(self, text: str) -> str:
        return ANNOTATED_DISCORD_REFERENCE_RE.sub(r"<\1\2>", text)

    def strip_context_tag_namespaces(self, text: str) -> str:
        text = _CTX_OPEN_TAG_NAMESPACE_RE.sub("<", text)
        return _CTX_CLOSE_TAG_NAMESPACE_RE.sub("</", text)

    def _discord_reference_name(self, entity: 'discord.User | discord.Member | discord.Role | discord.abc.GuildChannel | discord.Thread') -> str:
        if isinstance(entity, discord.Member) or isinstance(entity, discord.User):
            return entity.display_name
        return entity.name

    def _compact_description(self, text: str) -> str:
        text = " ".join(text.split())
        return text[:180]

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

    def _unique_tool_name(self, name: str) -> str:
        base = re.sub(r"\W+", "_", name.casefold()).strip("_") or "action"
        if base[0].isdigit():
            base = f"action_{base}"
        used = {action.tool_name for action in self._actions}
        tool_name = base
        index = 2
        while tool_name in used:
            tool_name = f"{base}_{index}"
            index += 1
        return tool_name

    def _normalize_params(
        self,
        params: AIParamsTable,
    ) -> dict[str, AIParam]:
        normalized: dict[str, AIParam] = {}
        for name, param in params.items():
            if not isinstance(param, AIParam):
                raise TypeError(f"AI parameter {name} must be an AIParam, got {type(param).__name__}")
            if not self._supported_param_type(param.type):
                raise TypeError(f"Unsupported AI parameter type for {name}: {param.type!r}")
            normalized[name] = param
        return normalized

    def _supported_param_type(self, annotation: object) -> bool:
        non_none = self._non_none_type(annotation)
        return (
            non_none in (str, int, float, bool, object, None, type(None)) or
            self._literal_choices(non_none) is not None or
            self._is_discord_user_type(non_none)
        )

    def _literal_choices(self, annotation: object) -> list[str] | None:
        target_type = self._non_none_type(annotation)
        if get_origin(target_type) is not Literal:
            return None
        choices: list[str] = []
        for choice in get_args(target_type):
            if not isinstance(choice, str):
                return None
            choices.append(choice)
        return choices

    def _allows_none(self, annotation: object) -> bool:
        return annotation in (None, type(None)) or type(None) in get_args(annotation)

    def _non_none_type(self, annotation: object) -> object:
        if self._is_union(annotation):
            args = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(args) == 1:
                return args[0]
        return annotation

    def _is_discord_user_type(self, annotation: object) -> bool:
        if annotation in (discord.User, discord.Member):
            return True
        if self._is_union(annotation):
            args = set(get_args(annotation))
            return bool(args & {discord.User, discord.Member}) and args <= {
                discord.User,
                discord.Member,
                type(None),
            }
        return False

    def _is_union(self, annotation: object) -> bool:
        return get_origin(annotation) in (Union, types.UnionType)


class _Missing:
    pass


_MISSING = _Missing()

ai = AICore[
    ai_plugin.BotActionParameters,
    ai_plugin.BotAction
]()


def action(
    name: str,
    description: str,
    command_name: str | None = None,
    params: AIParamsTable | None = None,
    **kwargs: ai_plugin.BotActionParameters,
) -> Callable[[ai_plugin.BotAction], ai_plugin.BotAction]:
    return ai.action(name, description, command_name=command_name, params=params, **kwargs)
