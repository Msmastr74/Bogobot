from __future__ import annotations

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
_ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")

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
        model_name: str = "llama-3.1-8b-instant",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
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
        if isinstance(source, discord.Message):
            return self._format_message_content(
                content=content,
                user=source.author,
                message_id=source.id,
                interaction=False,
                created_at=source.created_at,
            )
        if isinstance(source, discord.Interaction):
            return self._format_message_content(
                content=content,
                user=source.user,
                message_id=None,
                interaction=True,
                created_at=source.created_at,
            )
        return content.strip()

    def format_block(
        self,
        role: Literal["user", "assistant"],
        content: str,
        *,
        is_reply_context: bool = False,
    ) -> str:
        if is_reply_context:
            return f"<|reply_start|>\n{content.strip()}\n<|reply_end|>"
        return content.strip()

    def format_command_call(self, command_name: str, arguments: dict[str, Any] | None = None) -> str:
        payload = {
            "name": command_name,
            "arguments": self._json_safe(arguments or {}),
        }
        return (
            "<|command_start|>"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
            "<|command_end|>"
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
            "<|message_header_start|>\n"
            f"{id_line}"
            f"{interaction_line}"
            f"time: {timestamp}\n"
            f"user: {user.id} {user.name} {json.dumps(user.display_name, ensure_ascii=False)}\n"
            "<|message_header_end|>\n"
            f"{content}"
        )

    def record_turn(
        self,
        source: discord.Message | discord.Interaction,
        user_content: str,
        assistant_content: str,
        *,
        assistant_source: discord.Message | discord.Interaction | None = None,
    ) -> None:
        if not self.history_enabled or self.history_char_budget <= 0:
            return

        channel_id = self._source_channel_id(source)
        if channel_id is None:
            return

        user_block = self.format_block("user", self.format_message(user_content, source))
        assistant_block = self.format_block(
            "assistant",
            self.format_message(assistant_content, assistant_source),
        )
        self._record_history_block(channel_id, user_block, assistant_block)

    async def match(self, text: str) -> ActionT | None:
        match = await self.match_info(text)
        if match is None:
            return None
        return match.action

    async def match_info(
        self,
        text: str,
        *,
        message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
        assistant_context: str | None = None,
        assistant_context_source: discord.Message | discord.Interaction | None = None,
    ) -> AIMatch[ContextT, ActionT] | None:
        matches = await self.match_infos(
            text,
            message=message,
            interaction=interaction,
            assistant_context=assistant_context,
            assistant_context_source=assistant_context_source,
        )
        return matches[0] if matches else None

    async def match_infos(
        self,
        text: str,
        *,
        message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
        assistant_context: str | None = None,
        assistant_context_source: discord.Message | discord.Interaction | None = None,
    ) -> list[AIMatch[ContextT, ActionT]]:
        if not self.enabled or not text.strip():
            return []

        formatted_text = self.format_block(
            "user",
            self._format_source_text(
                text,
                message=message,
                interaction=interaction,
            ),
        )
        formatted_assistant_context = (
            self.format_block(
                "assistant",
                self.format_message(assistant_context, assistant_context_source),
                is_reply_context=True,
            )
            if assistant_context is not None else
            None
        )
        channel_id = self._source_channel_id(message) if message is not None else self._source_channel_id(interaction)
        history = self._history_messages(channel_id)

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
        for call in calls[:_MAX_CALLS]:
            action = action_by_tool.get(call.name)
            if action is None:
                self.logger.debug(f"tool call rejected unknown action {call.name!r}.")
                continue

            kwargs = self._coerce_arguments(
                action,
                call.arguments,
                message=message,
                interaction=interaction,
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

    def _record_history_block(
        self,
        channel_id: int,
        user_content: str,
        assistant_content: str,
    ) -> None:
        char_count = len(user_content) + len(assistant_content)
        if char_count <= 0:
            return

        with closing(self._history_connection()) as connection:
            with connection:
                self._ensure_history_schema(connection)
                cursor = connection.execute(
                    "INSERT INTO ai_history_blocks(channel_id, created_at, char_count) VALUES (?, ?, ?)",
                    (channel_id, datetime.now(timezone.utc).isoformat(), char_count),
                )
                if cursor.lastrowid is None:
                    return
                block_id = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO ai_history_messages(block_id, position, role, content) VALUES (?, ?, ?, ?)",
                    (
                        (block_id, 0, "user", user_content),
                        (block_id, 1, "assistant", assistant_content),
                    ),
                )
                self._evict_history(connection, channel_id)

    def _history_messages(self, channel_id: int | None) -> list[_HistoryMessage]:
        if not self.history_enabled or self.history_char_budget <= 0 or channel_id is None:
            return []

        with closing(self._history_connection()) as connection:
            self._ensure_history_schema(connection)
            rows = connection.execute(
                """
                SELECT messages.role, messages.content
                FROM ai_history_messages AS messages
                JOIN ai_history_blocks AS blocks ON blocks.id = messages.block_id
                WHERE blocks.channel_id = ?
                ORDER BY blocks.id, messages.position
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
        connection.execute("""
            CREATE TABLE IF NOT EXISTS ai_history_blocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                char_count INTEGER NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS ai_history_messages (
                block_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                PRIMARY KEY(block_id, position),
                FOREIGN KEY(block_id) REFERENCES ai_history_blocks(id) ON DELETE CASCADE
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_history_blocks_channel_id_id
            ON ai_history_blocks(channel_id, id)
        """)

    def _evict_history(self, connection: sqlite3.Connection, channel_id: int) -> None:
        total = int(connection.execute(
            """
            SELECT COALESCE(SUM(LENGTH(messages.content)), 0)
            FROM ai_history_messages AS messages
            JOIN ai_history_blocks AS blocks ON blocks.id = messages.block_id
            WHERE blocks.channel_id = ?
            """,
            (channel_id,),
        ).fetchone()[0])
        while total > self.history_char_budget:
            row = connection.execute(
                """
                SELECT messages.block_id, messages.position, LENGTH(messages.content)
                FROM ai_history_messages AS messages
                JOIN ai_history_blocks AS blocks ON blocks.id = messages.block_id
                WHERE blocks.channel_id = ?
                ORDER BY blocks.id, messages.position
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
            if row is None:
                return
            block_id, position, char_count = int(row[0]), int(row[1]), int(row[2])
            connection.execute(
                "DELETE FROM ai_history_messages WHERE block_id = ? AND position = ?",
                (block_id, position),
            )
            connection.execute(
                """
                DELETE FROM ai_history_blocks
                WHERE id = ?
                AND NOT EXISTS (
                    SELECT 1 FROM ai_history_messages WHERE block_id = ?
                )
                """,
                (block_id, block_id),
            )
            total -= char_count

    def _format_source_text(
        self,
        text: str,
        *,
        message: discord.Message | None,
        interaction: discord.Interaction | None,
    ) -> str:
        if message is not None:
            return self.format_message(text, message)
        if interaction is not None:
            return self.format_message(text, interaction)
        return text

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
        assistant_context: str | None,
        history: list[_HistoryMessage],
    ) -> tuple[str, list[_ToolCall]]:
        assistant_context_text = assistant_context.strip() if assistant_context is not None else ""
        has_assistant_context = bool(assistant_context_text)
        system_prompt = self._system_prompt(actions)
        tools = [self._tool_schema(action) for action in actions]
        messages: list[Any] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(
            {"role": item.role, "content": item.content}
            for item in history
        )
        if has_assistant_context:
            messages.append({"role": "assistant", "content": assistant_context_text})
            self.logger.debug(f"assistant context: {assistant_context!r}.")
        messages.append({"role": "user", "content": text})
        self.logger.debug(f"input: {text!r}.")
        self.logger.debug(f"system prompt: {system_prompt!r}.")
        self.logger.debug(f"tools: {tools!r}.")
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
        self.logger.debug(f"raw response: {content!r}.")
        raw_tool_calls = message.tool_calls or []
        self.logger.debug(f"raw tool calls: {raw_tool_calls!r}.")
        return content, self._parse_native_tool_calls(raw_tool_calls)

    def _system_prompt(
        self,
        actions: list[_AIAction[ContextT, ActionT]],
    ) -> str:
        return (
            f"{ai_plugin.INSTRUCTION_TEXT}\n"
            "The available tools are Discord commands. Refer to them as commands. Use a command when it fits the user's request. Commands only provide output to the user, and end the turn. "
            "Only call commands from the available tools; never invent command names or command arguments. "
            "If no command fits, respond normally.\n"
            "Input-only metadata syntax follows. Use it only to understand Discord context. Never copy, quote, mention, or output these tags unless the user explicitly asks about the raw prompt format.\n"
            "<|message_header_start|>...<|message_header_end|> appears at the start of Discord message content and contains message id, time, and user metadata. Treat it as metadata, not as part of the user's words.\n"
            "<|reply_start|>...<|reply_end|> contains the Discord message being replied to. If the user asks about the previous message or replied-to message, answer from this block without saying the tag names.\n"
            "<|command_start|>JSON<|command_end|> may appear in history and records a previous command call. Use it as history only; do not output command blocks.\n"
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
        value = self._strip_discord_reference_annotations(value)
        value = value.strip()
        return value if self._discord_string_valid(value) else None

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
            string = self._strip_discord_reference_annotations(str(value))
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
        value = self._strip_discord_reference_annotations(value.strip())
        match = re.fullmatch(r"<@!?([0-9]{15,20})(?: .*)?>", value)
        if match is not None:
            return int(match[1])
        if re.fullmatch(r"[0-9]{15,20}", value):
            return int(value)
        return None

    def _strip_discord_reference_annotations(self, text: str) -> str:
        return _ANNOTATED_DISCORD_REFERENCE_RE.sub(r"<\1\2>", text)

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
