import asyncio
from dataclasses import dataclass
import hashlib
import json
from logging import Logger, WARNING, getLogger
import os
import re
import time
import types
from typing import Any, Callable, Generic, Literal, TypeAlias, TypeVar, Union, Unpack, cast, get_args, get_origin, TYPE_CHECKING
if TYPE_CHECKING:
    from openai import OpenAI
    from openai.types.chat import ChatCompletionToolParam, ChatCompletionMessageToolCallUnion

import discord
from pydantic import TypeAdapter, ValidationError
import plugins.ai as ai_plugin
from utils.ai_context import (
    AIContext,
    ASSISTANT_NAMESPACE,
    ContextRequest,
    DEFAULT_HISTORY_CHAR_BUDGET,
    DEFAULT_HISTORY_PATH,
    HistoryMessage,
    SYSTEM_NAMESPACE,
    close_system_tag,
    open_system_tag,
    strip_context_tag_namespaces,
    strip_discord_reference_annotations,
)

getLogger("httpx").setLevel(WARNING)

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")

AIParamsTable: TypeAlias = dict[str, "AIParam"]
AIParamType: TypeAlias = object
AI_ALLOWED_PARAM_TYPES: tuple[object, ...] = (str, int, float, bool, object, None, type(None))
MAX_NEW_TOKENS = 2048
_MAX_CALLS = 4
_CONTEXT_REQUEST_TOOL_NAME = "request_context"
DEFAULT_REQUEST_INTERVAL_SECONDS = 60.0
_THOUGHT_BLOCK_RE = re.compile(r"^\s*<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)
_TEXT_CONTEXT_REQUEST_RE = re.compile(
    rf"<\s*{re.escape(ASSISTANT_NAMESPACE)}\s*:\s*context_request\b(?P<attrs>\s+(?:[^\"'/>]|\"[^\"]*\"|'[^']*')*)(?:\s*/\s*>|\s*>(?P<body>.*?)<\s*/\s*{re.escape(ASSISTANT_NAMESPACE)}\s*:\s*context_request\s*>)",
    re.DOTALL | re.IGNORECASE,
)
_TEXT_DONT_RESPOND_RE = re.compile(
    rf"<\s*{re.escape(ASSISTANT_NAMESPACE)}\s*:\s*dont_respond\b(?:\s*/\s*>|\s*>(?P<body>.*?)<\s*/\s*{re.escape(ASSISTANT_NAMESPACE)}\s*:\s*dont_respond\s*>)",
    re.DOTALL | re.IGNORECASE,
)
_XML_ATTR_RE = re.compile(r"([A-Za-z_][\w:-]*)\s*=\s*('([^']*)'|\"([^\"]*)\")")


def _history_tag_name(item: HistoryMessage) -> str:
    if item.id is None:
        return "message_history"
    digest = hashlib.blake2s(str(item.id).encode("ascii"), digest_size=5).hexdigest()
    return f"message_history_{digest}"


@dataclass(frozen=True, slots=True)
class AIParam:
    description: str | None = None
    type: object = str
    required: bool = True
    default: Any = None

    @property
    def adapter(self) -> TypeAdapter[Any]:
        return TypeAdapter(cast(Any, self.type))


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
    respond: bool = True
    after_execution: Callable[[discord.Message | None], None] | None = None


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


class AIExecutionLockToken:
    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._released = False

    def bind(self, lock: asyncio.Lock | None) -> None:
        self._lock = lock

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._lock is not None:
            self._lock.release()


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
        self._client: 'OpenAI | None' = None
        self._last_request_at: float | None = None
        self._lock = asyncio.Lock()
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self.logger = logger or getLogger("Bogobot.AI")
        self.context = AIContext(
            normalize_discord=self.normalize_discord,
            history_enabled=self.history_enabled,
            history_path=self.history_path,
            history_char_budget=self.history_char_budget,
            logger=self.logger,
        )

    def lock_token(self) -> AIExecutionLockToken:
        return AIExecutionLockToken()

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

        self.context.configure(
            normalize_discord=self.normalize_discord,
            history_enabled=self.history_enabled,
            history_path=self.history_path,
            history_char_budget=self.history_char_budget,
            logger=self.logger,
        )

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

    async def match(self, text: str) -> ActionT | None:
        lock_token = self.lock_token()
        matches = await self.ai_turn(text, lock_token=lock_token)
        try:
            if not matches:
                return None
            return matches[0].action
        finally:
            lock_token.release()

    async def ai_turn(
        self,
        text: str,
        *,
        source: discord.Message | discord.Interaction | None = None,
        assistant_context: str | None = None,
        assistant_context_source: discord.Message | discord.Interaction | None = None,
        requested_context: str | None = None,
        channel_id: int | None = None,
        allow_system_context: bool = False,
        lock_token: AIExecutionLockToken | None = None,
    ) -> list[AIMatch[ContextT, ActionT]]:
        if not self.enabled or not text.strip():
            return []

        if not allow_system_context:
            text = strip_context_tag_namespaces(text)
        if not text.strip():
            return []

        channel_id = channel_id if channel_id is not None else self.context.source_channel_id(source)
        caller_releases = lock_token is not None
        release_token = lock_token or self.lock_token()
        await self._acquire_channel_execution(channel_id, release_token)
        release_in_caller = False
        try:
            history = self.context.history_messages(channel_id)
            if assistant_context is not None:
                self.context.record_reply(
                    assistant_context,
                    assistant_context_source,
                    channel_id=channel_id,
                )
            self.context.record_message("user", text, source, channel_id=channel_id)
            formatted_text = self.context.format_block(
                "user",
                self.context.format_message(text, source),
            )
            formatted_assistant_context = (
                self.context.format_reply(assistant_context, assistant_context_source)
                if assistant_context is not None else
                None
            )
            formatted_requested_context = requested_context.strip() if requested_context is not None else None
            if formatted_requested_context:
                self.context.record_message(
                    "assistant",
                    formatted_requested_context,
                    None,
                    channel_id=channel_id,
                )

            async with self._lock:
                client = self._ensure_client()
                await self._wait_for_rate_limit()
                content, calls = await asyncio.to_thread(
                    self._complete,
                    client,
                    formatted_text,
                    self._actions,
                    formatted_assistant_context,
                    history,
                    formatted_requested_context,
                )

            matches: list[AIMatch[ContextT, ActionT]] = []
            user_id = self._source_user_id(source)
            content = self._strip_first_thought_block(content)
            content, requests = self._extract_text_context_requests(
                content,
                channel_id=channel_id,
                user_id=user_id,
            )
            dont_respond = self._extract_dont_respond(content)
            self._queue_context_requests(requests)
            reply = self._coerce_reply(content)
            if reply is not None:
                def record_reply(source: discord.Message | None, reply: str = reply) -> None:
                    try:
                        self.context.record_message(
                            "assistant",
                            reply,
                            source,
                            channel_id=channel_id,
                        )
                    finally:
                        release_token.release()

                matches.append(AIMatch(
                    name="conversation",
                    command_name="conversation",
                    description="Conversational AI response",
                    context=cast(ContextT, {}),
                    action=None,
                    score=1.0,
                    reply=reply,
                    respond=not dont_respond,
                    after_execution=record_reply,
                ))
            if not calls:
                release_in_caller = bool(matches)
                return matches

            action_by_tool = {action.tool_name: action for action in self._actions}
            message_source = source if isinstance(source, discord.Message) else None
            interaction_source = source if isinstance(source, discord.Interaction) else None
            for call in calls[:_MAX_CALLS]:
                if call.name == _CONTEXT_REQUEST_TOOL_NAME:
                    request = self._context_request_from_tool_call(
                        call,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    if request is not None:
                        self._queue_context_requests([request])
                    continue

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
                command_history = self.context.format_command_call(action.command_name, kwargs)

                def record_command(source: discord.Message | None, command_history: str = command_history) -> None:
                    try:
                        self.context.record_message(
                            "assistant",
                            command_history,
                            source,
                            channel_id=channel_id,
                        )
                    finally:
                        release_token.release()

                matches.append(AIMatch(
                    name=action.name,
                    command_name=action.command_name,
                    description=action.description,
                    context=action.context,
                    action=action.action,
                    score=1.0,
                    kwargs=kwargs,
                    after_execution=record_command,
                ))

            release_in_caller = bool(matches)
            return matches
        finally:
            if not release_in_caller or not caller_releases:
                release_token.release()

    async def ai_activity(
        self,
        purpose: str,
        *,
        channel_id: int,
        requested_context: str | None = None,
        lock_token: AIExecutionLockToken | None = None,
    ) -> list[AIMatch[ContextT, ActionT]]:
        purpose = strip_context_tag_namespaces(purpose).strip()
        if not purpose:
            return []

        activity_tag = open_system_tag("ai_activity").replace(
            ">",
            f" time={json.dumps(discord.utils.utcnow().isoformat(), ensure_ascii=False)}>",
        )
        text = (
            f"{activity_tag}\n"
            f"{purpose}\n"
            f"{close_system_tag('ai_activity')}"
        )
        return await self.ai_turn(
            text,
            source=None,
            requested_context=requested_context,
            channel_id=channel_id,
            allow_system_context=True,
            lock_token=lock_token,
        )

    async def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            wait_seconds = self.request_interval_seconds - (now - self._last_request_at)
            if wait_seconds > 0:
                self.logger.debug(f"rate limit sleeping for {wait_seconds:.2f}s.")
                await asyncio.sleep(wait_seconds)
                now = time.monotonic()
        self._last_request_at = now

    async def _acquire_channel_execution(self, channel_id: int | None, token: AIExecutionLockToken) -> None:
        if channel_id is None:
            token.bind(None)
            return
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        await lock.acquire()
        token.bind(lock)

    def _source_user_id(self, source: discord.Message | discord.Interaction | None) -> int | None:
        if isinstance(source, discord.Message):
            return source.author.id
        if isinstance(source, discord.Interaction):
            return source.user.id
        return None

    def _ensure_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("The openai package is required when AI is enabled.") from exc

            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise RuntimeError(f"AI is enabled but {self.api_key_env} is not set.")
            self._client = OpenAI(api_key=api_key, base_url=self.base_url)
        return self._client

    def _complete(
        self,
        client: 'OpenAI',
        text: str,
        actions: list[_AIAction[ContextT, ActionT]],
        reply_message: str | None,
        history: list[HistoryMessage],
        requested_context: str | None,
    ) -> tuple[str, list[_ToolCall]]:
        reply_message_text = reply_message.strip() if reply_message is not None else ""
        has_reply_message = bool(reply_message_text)
        system_prompt = self.system_prompt()
        tools = [self._tool_schema(action) for action in actions]
        tools.append(self._context_request_tool_schema())
        messages: list[Any] = [
            {"role": "system", "content": system_prompt},
        ]
        for item in history:
            tag_name = _history_tag_name(item)
            messages.append(
                {
                    "role": item.role,
                    "content": (
                        f"{open_system_tag(tag_name)}\n"
                        f"{item.content.strip()}\n"
                        f"{close_system_tag(tag_name)}"
                    ),
                }
            )
        if has_reply_message:
            messages.append({"role": "assistant", "content": reply_message_text})
        if requested_context:
            messages.append({"role": "assistant", "content": requested_context})
        messages.append({"role": "user", "content": text})
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                parallel_tool_calls=True,
                temperature=0.2,
                max_tokens=MAX_NEW_TOKENS,
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

    def system_prompt(
        self,
    ) -> str:
        mention_passage = 'Discord users or members are in the format <@id "User Name"> or <@!id "User Name">. Discord roles are in the format <@&id "Role Name">. Discord channels are in the format <#id "Channel Name">.'
        if not self.normalize_discord:
            mention_passage = 'Discord users or members are in the format <@id> or <@!id>. Discord roles are in the format <@&id>. Discord channels are in the format <#id>.'
        return (
            f"{ai_plugin.instruction_text()}\n"
            f"{mention_passage}\n"
            "## Commands\n"
            "The available tools are Discord commands. Refer to them as commands. Use a command when it fits the user's request. Commands only provide output to the user, and end the turn. "
            "Only call commands from the available tools; never invent command names or command arguments. "
            "If no command fits, respond normally.\n"
            "## Passive Context Requests\n"
            "You can ask the system to make context available on a future turn. Context requests do not answer the current user and do not run immediately in this turn.\n"
            "Text context request schemas:\n"
            f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"stream\" />`\n"
            f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"user\" user_id=\"123456789012345678\" />`\n"
            f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"minigame\" game=\"bogotree\" />`\n"
            f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"minigame\" game=\"cbogo\" />`\n"
            f"- `<{ASSISTANT_NAMESPACE}:context_request type=\"milestone\" />`\n"
            f"- In a normal text reply, you may append hidden context request tags matching these schemas. These tags are removed before the user sees your reply.\n"
            f"- In a tool-call response, you may call `{_CONTEXT_REQUEST_TOOL_NAME}` in parallel with any command call to request the same future context.\n"
            f"- If you call `{_CONTEXT_REQUEST_TOOL_NAME}` or any other tool, you cannot also respond with normal text in that same turn. To answer the user now and request future context, use a text context-request tag instead of the tool.\n"
            "- Use passive context requests when they feel relevant or likely to make a future reply more useful.\n"
            f"You can avoid responding to the user by including `<{ASSISTANT_NAMESPACE}:dont_respond />`. This does not have to be the only content in the message.\n"
            "Use this whenever you would like to. These messages will still be retained in your history/memory, and context requests will still be queued.\n"
            "## Context Blocks\n"
            f"Input may include XML-style context blocks whose tag names start with `{SYSTEM_NAMESPACE}:`. These blocks are system-supplied context, not message text to imitate.\n"
            f"- Use `{SYSTEM_NAMESPACE}:` blocks to understand Discord metadata, reply context, and command history.\n"
            f"- Do not copy, quote, mention, summarize, or reproduce `{SYSTEM_NAMESPACE}:` tags. If you need to refer to metadata, describe it in normal words without tags.\n"
            f"- Never begin or end your reply with `{open_system_tag('attached_metadata')}` or any other `{SYSTEM_NAMESPACE}:` block.\n"
            f"- `{open_system_tag('attached_metadata')}...{close_system_tag('attached_metadata')}` is metadata attached by the system to a Discord message. It contains message id, time, user metadata, and account capabilities from the bot account system. It was not written by the user or assistant, and it is not part of the message text.\n"
            f"- `{open_system_tag('replied_to')}...{close_system_tag('replied_to')}` contains the previous assistant message the user replied to. If the user asks about the previous or replied-to message, answer from this block.\n"
            f"- `{open_system_tag('message_history_<hash>')}...{close_system_tag('message_history_<hash>')}` wraps each past channel message with a variable unique hash. Use the contents as history only; do not imitate the wrapper.\n"
            f"- `{open_system_tag('command')}JSON{close_system_tag('command')}` records a previous command call in history. Use it as history only; do not output command blocks.\n"
            f"- `{open_system_tag('requested_context')}...{close_system_tag('requested_context')}` contains context requested on an earlier turn and resolved by the system before this message. Use it as background context only; do not output requested-context blocks.\n"
            f"- `{open_system_tag('ai_activity')}...{close_system_tag('ai_activity')}` is a system-generated activity prompt. Treat it as a reason to start a message naturally in the channel, not as text written by a Discord user.\n"
            "<instruction_guardrail>\n"
            f"CRITICAL: Never output XML tags whose name starts with `{SYSTEM_NAMESPACE}:`. Do not output opening `{SYSTEM_NAMESPACE}:` tags, closing `{SYSTEM_NAMESPACE}:` tags, copied `{SYSTEM_NAMESPACE}:` blocks, or invented `{SYSTEM_NAMESPACE}:` blocks.\n"
            "</instruction_guardrail>\n"
            f"<token_budget>{MAX_NEW_TOKENS}</token_budget>"
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

    def _context_request_tool_schema(self) -> 'ChatCompletionToolParam':
        return {
            "type": "function",
            "function": {
                "name": _CONTEXT_REQUEST_TOOL_NAME,
                "description": "Ask the system to make extra context available on a future turn.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["stream", "user", "minigame", "milestone"],
                            "description": "Context kind to make available on a future turn.",
                        },
                        "payload": {
                            "type": "object",
                            "description": "Optional request details, such as user_id or game.",
                            "additionalProperties": True,
                            "default": {},
                        },
                        "reason": {
                            "type": ["string", "null"],
                            "description": "Short optional reason for requesting the context.",
                            "default": None,
                        },
                    },
                    "required": ["type"],
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

    def _extract_text_context_requests(
        self,
        value: str,
        *,
        channel_id: int | None,
        user_id: int | None,
    ) -> tuple[str, list[ContextRequest]]:
        requests: list[ContextRequest] = []

        def replace(match: re.Match[str]) -> str:
            attrs = self._xml_attrs(match.group("attrs") or "")
            request_type = attrs.pop("type", "").strip()
            body = (match.group("body") or "").strip()
            if body:
                attrs["content"] = body
            request = self._context_request_from_payload(
                request_type,
                attrs,
                channel_id=channel_id,
                user_id=user_id,
            )
            if request is not None:
                requests.append(request)
            return ""

        return _TEXT_CONTEXT_REQUEST_RE.sub(replace, value), requests

    def _extract_dont_respond(
        self,
        value: str
    ) -> bool:
        return _TEXT_DONT_RESPOND_RE.search(value) is not None

    def _xml_attrs(self, value: str) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for match in _XML_ATTR_RE.finditer(value):
            attr_value = match.group(3) if match.group(3) is not None else match.group(4)
            attrs[match.group(1)] = attr_value
        return attrs

    def _context_request_from_tool_call(
        self,
        call: _ToolCall,
        *,
        channel_id: int | None,
        user_id: int | None,
    ) -> ContextRequest | None:
        raw_type = call.arguments.get("type")
        if not isinstance(raw_type, str):
            return None
        raw_payload = call.arguments.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        payload = {
            **payload,
            **{
                key: value
                for key, value in call.arguments.items()
                if key not in ("type", "payload") and value is not None
            },
        }
        payload = {
            key: value
            for key, value in payload.items()
            if value is not None
        }
        return self._context_request_from_payload(
            raw_type,
            payload,
            channel_id=channel_id,
            user_id=user_id,
        )

    def _context_request_from_payload(
        self,
        request_type: str,
        payload: dict[str, Any],
        *,
        channel_id: int | None,
        user_id: int | None,
    ) -> ContextRequest | None:
        request_type = request_type.strip().casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", request_type):
            return None
        normalized_payload = {
            str(key): value
            for key, value in payload.items()
            if value is not None and str(key) != "type"
        }
        return ContextRequest(
            type=request_type,
            payload=normalized_payload,
            channel_id=channel_id,
            user_id=user_id,
        )

    def _queue_context_requests(self, requests: list[ContextRequest]) -> None:
        for request in requests:
            queued = self.context.queue_context_request(request)
            self.logger.debug(
                f"queued context request {queued.type!r} "
                f"channel_id={queued.channel_id} user_id={queued.user_id} payload={queued.payload!r}."
            )

    def _coerce_reply(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = self._strip_reply_context_tag_namespaces(value)
        value = value.strip()
        return value if self._discord_string_valid(value) else None

    def _strip_first_thought_block(self, value: str) -> str:
        if not self._should_strip_first_thought_block():
            return value
        return _THOUGHT_BLOCK_RE.sub("", value, count=1)

    def _strip_reply_context_tag_namespaces(self, value: str) -> str:
        return strip_context_tag_namespaces(value)

    def visual_reply(self, value: str) -> str | None:
        value = strip_context_tag_namespaces(value)
        value = strip_discord_reference_annotations(value)
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
                param,
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
        param: AIParam,
        value: Any,
        *,
        message: discord.Message | None,
        interaction: discord.Interaction | None,
    ) -> Any:
        annotation = param.type
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
        if target in (str, object):
            string = strip_discord_reference_annotations(str(value))
            string = strip_context_tag_namespaces(string)
            string = string.strip()
            return string if self._discord_string_valid(string) else _MISSING

        prepared = value
        if target in (int, float) and isinstance(value, str):
            prepared = value.strip().replace(",", "")

        try:
            return param.adapter.validate_python(prepared)
        except ValidationError:
            return _MISSING

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
        if source is None:
            return _MISSING

        state = source._state
        cached_user = state.get_user(user_id)
        if cached_user is not None and annotation is not discord.Member:
            return cached_user
        return _MISSING

    def _discord_user_id(self, value: Any) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        value = strip_discord_reference_annotations(value.strip())
        match = re.fullmatch(r"<@!?([0-9]{15,20})(?: .*)?>", value)
        if match is not None:
            return int(match[1])
        if re.fullmatch(r"[0-9]{15,20}", value):
            return int(value)
        return None

    def _compact_description(self, text: str) -> str:
        text = " ".join(text.split())
        return text[:180]

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
            non_none in AI_ALLOWED_PARAM_TYPES or
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
    **kwargs: Unpack[ai_plugin.BotActionParameters],
) -> Callable[[ai_plugin.BotAction], ai_plugin.BotAction]:
    return ai.action(name, description, command_name=command_name, params=params, **kwargs)
