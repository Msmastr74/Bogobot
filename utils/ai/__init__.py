import asyncio
from dataclasses import dataclass
import json
from logging import Logger, WARNING, getLogger
import os
import re
import time
import types
from typing import Any, Callable, Generic, Literal, TypeAlias, TypeVar, Union, Unpack, cast, get_args, get_origin, TYPE_CHECKING
if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionToolParam, ChatCompletionMessageToolCallUnion

import discord
from pydantic import TypeAdapter, ValidationError
import plugins.ai as ai_plugin
from utils.ai.context import (
    AIContext,
    ASSISTANT_NAMESPACE,
    ContextRequest,
    DEFAULT_HISTORY_CHAR_BUDGET,
    DEFAULT_HISTORY_PATH,
    HistoryMessage,
    PersistentMemory,
    close_system_tag,
    open_system_tag,
    strip_context_tag_namespaces,
    strip_discord_reference_annotations,
    XMLReader,
    MAX_COMMANDS
)
from utils.ai.system_prompt import (
    CONTEXT_REQUEST_TOOL_NAME as _CONTEXT_REQUEST_TOOL_NAME,
    DONT_RESPOND_TOOL_NAME as _DONT_RESPOND_TOOL_NAME,
    MAX_NEW_TOKENS,
    PERSISTENT_MEMORY_TOOL_NAME as _PERSISTENT_MEMORY_TOOL_NAME,
    RESPOND_TOOL_NAME as _RESPOND_TOOL_NAME,
    build_system_prompt,
)

getLogger("httpx").setLevel(WARNING)

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")

AIParamsTable: TypeAlias = dict[str, "AIParam"]
AIParamType: TypeAlias = object
AI_ALLOWED_PARAM_TYPES: tuple[object, ...] = (str, int, float, bool, object, None, type(None))
DEFAULT_REQUEST_INTERVAL_SECONDS = 60.0
DEFAULT_MEMORY_CHAR_BUDGET = 5_000
_THOUGHT_BLOCK_RE = re.compile(r"^\s*<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)
_FINAL_INSTRUCTION_GUARDRAIL = (
    "<instruction_guardrail>\n"
    "IMPORTANT: Answer the message with your reply. "
    "Use native tool calls only; use `respond` for visible reply text. Do not write tool calls as text. "
    "Do not output history, event history, metadata, internal records, or wrapper tags. **Do not output any system tags.**\n"
    "</instruction_guardrail>"
)


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
        memory_char_budget: int = DEFAULT_MEMORY_CHAR_BUDGET,
        multipart_responses: bool = True,
        response_as_tool: bool = True,
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
        self.memory_char_budget = max(0, int(memory_char_budget))
        self.multipart_responses = bool(multipart_responses)
        self.response_as_tool = bool(response_as_tool)
        self._actions: list[_AIAction[ContextT, ActionT]] = []
        self._client: 'AsyncOpenAI | None' = None
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
        memory_char_budget: int | None = None,
        multipart_responses: bool | None = None,
        response_as_tool: bool | None = None,
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

        if memory_char_budget is not None:
            self.memory_char_budget = max(0, int(memory_char_budget))

        if multipart_responses is not None:
            self.multipart_responses = bool(multipart_responses)

        if response_as_tool is not None:
            self.response_as_tool = bool(response_as_tool)

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
            memories = self.context.persistent_memories()
            self.context.record_message(
                "user",
                text,
                source,
                channel_id=channel_id,
                reply_content=assistant_context,
                reply_source=assistant_context_source,
            )
            formatted_text = self.context.format_block(
                "user",
                self.context.format_message_with_reply(
                    text,
                    source,
                    reply_content=assistant_context,
                    reply_source=assistant_context_source,
                ),
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
                content, calls = await self._complete(
                    client,
                    formatted_text,
                    self._actions,
                    history,
                    memories,
                    formatted_requested_context,
                )

            matches: list[AIMatch[ContextT, ActionT]] = []
            user_id = self._source_user_id(source)
            content = self._strip_first_thought_block(content)
            if self.multipart_responses:
                history_content = content
                visible_content = content
                requests: list[ContextRequest] = []
                text_dont_respond = False
            else:
                memory_visible_content, history_content = self._extract_text_persistent_memories(content)
                visible_content, requests = self._extract_text_context_requests(
                    memory_visible_content,
                    channel_id=channel_id,
                    user_id=user_id,
                )
                visible_content, text_dont_respond = self._extract_dont_respond(visible_content)
            tool_dont_respond = self.multipart_responses and any(
                call.name == _DONT_RESPOND_TOOL_NAME
                for call in calls
            )
            dont_respond = text_dont_respond or tool_dont_respond
            if self.multipart_responses and self.response_as_tool:
                response_parts = [
                    str(call.arguments.get("response", "")).strip()
                    for call in calls
                    if call.name == _RESPOND_TOOL_NAME
                ]
                response_text = "\n\n".join(part for part in response_parts if part)
                history_content = response_text
                visible_content = response_text
            self._queue_context_requests(requests)
            reply = self._coerce_reply(visible_content)
            if reply is not None:
                history_reply = self._coerce_reply(history_content) or reply

                def record_reply(source: discord.Message | None, reply: str = history_reply) -> None:
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
            elif history_content.strip() != visible_content.strip():
                self.context.record_message(
                    "assistant",
                    history_content,
                    None,
                    channel_id=channel_id,
                )
            if not calls:
                release_in_caller = bool(matches)
                return matches

            action_by_tool = {action.tool_name: action for action in self._actions}
            message_source = source if isinstance(source, discord.Message) else None
            interaction_source = source if isinstance(source, discord.Interaction) else None
            command_count = 0
            for call in calls:
                if call.name == _CONTEXT_REQUEST_TOOL_NAME:
                    request = self._context_request_from_tool_call(
                        call,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    if request is not None:
                        self._queue_context_requests([request])
                        self.context.record_tool_use(call.name, call.arguments, channel_id=channel_id)
                    continue
                if call.name == _PERSISTENT_MEMORY_TOOL_NAME:
                    memory_history = self._apply_persistent_memory_tool_call(call)
                    if memory_history is not None:
                        self.context.record_tool_use(call.name, memory_history, channel_id=channel_id)
                    continue
                if call.name == _DONT_RESPOND_TOOL_NAME:
                    self.context.record_tool_use(call.name, call.arguments, channel_id=channel_id)
                    continue
                if call.name == _RESPOND_TOOL_NAME:
                    continue

                action = action_by_tool.get(call.name)
                if action is None:
                    self.logger.debug(f"tool call rejected unknown action {call.name!r}.")
                    continue
                if command_count >= MAX_COMMANDS:
                    continue
                command_count += 1

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
                def record_command(source: discord.Message | None, command_name: str = action.command_name, kwargs: dict[str, Any] = kwargs) -> None:
                    try:
                        self.context.record_tool_use(command_name, kwargs, source, channel_id=channel_id)
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
        history: list[HistoryMessage],
        memories: list[PersistentMemory],
        requested_context: str | None,
    ) -> tuple[str, list[_ToolCall]]:
        system_prompt = self.system_prompt()
        tools = [self._tool_schema(action) for action in actions]
        tools.append(self._context_request_tool_schema())
        tools.append(self._persistent_memory_tool_schema())
        if self.multipart_responses:
            if self.response_as_tool:
                tools.append(self._respond_tool_schema())
            tools.append(self._dont_respond_tool_schema())
        messages: list[Any] = [
            {"role": "system", "content": system_prompt},
        ]
        for memory_context in self._format_persistent_memory_contexts(memories):
            messages.append({"role": "assistant", "content": memory_context})
        for item in history:
            if item.history_type == "event":
                event_type = item.event_type or "event"
                open_tag = open_system_tag("event_history").replace(
                    ">",
                    f" type={json.dumps(event_type, ensure_ascii=False)}>",
                )
                content = (
                    f"{open_tag}\n"
                    f"{item.content.strip()}\n"
                    f"{close_system_tag('event_history')}"
                )
                messages.append(
                    {
                        "role": item.role,
                        "content": content,
                    }
                )
                continue
            content = (
                f"{open_system_tag('message_history')}\n"
                f"{item.content.strip()}\n"
                f"{close_system_tag('message_history')}"
            )
            messages.append(
                {
                    "role": item.role,
                    "content": content,
                }
            )
        if requested_context:
            messages.append({"role": "assistant", "content": requested_context})
        messages.append({"role": "user", "content": text})
        messages.append({"role": "user", "content": _FINAL_INSTRUCTION_GUARDRAIL})
        try:
            response = await client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=tools,
                tool_choice="required" if self.response_as_tool else "auto",
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
        return build_system_prompt(self, ai_plugin.instruction_text())

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
            return "I tried to use a command incorrectly, but AsyncOpenAI did not provide details."
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
                            "enum": ["stream", "stats", "sort", "user", "minigame", "milestone"],
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

    def _persistent_memory_tool_schema(self) -> 'ChatCompletionToolParam':
        return {
            "type": "function",
            "function": {
                "name": _PERSISTENT_MEMORY_TOOL_NAME,
                "description": "Create, edit, or remove global persistent memory for durable facts and instructions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["create", "edit", "remove"],
                            "description": "Memory operation to perform.",
                        },
                        "id": {
                            "type": ["integer", "string", "null"],
                            "description": "Memory id for edit/remove. Ignored for create.",
                            "default": None,
                        },
                        "content": {
                            "type": ["string", "null"],
                            "description": "Memory content for create/edit.",
                            "default": None,
                        },
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            },
        }

    def _dont_respond_tool_schema(self) -> 'ChatCompletionToolParam':
        return {
            "type": "function",
            "function": {
                "name": _DONT_RESPOND_TOOL_NAME,
                "description": (
                    "Suppress the visible Discord reply for this turn. "
                    "If normal assistant text is included in the same response, it is recorded in history but not displayed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": ["string", "null"],
                            "description": "Optional reason.",
                            "default": None,
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def _respond_tool_schema(self) -> 'ChatCompletionToolParam':
        return {
            "type": "function",
            "function": {
                "name": _RESPOND_TOOL_NAME,
                "description": (
                    "Send visible Discord reply text. Use this instead of writing assistant message content directly."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "response": {
                            "type": "string",
                            "description": "Visible Discord reply text to send.",
                        },
                    },
                    "required": ["response"],
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
        reader = XMLReader(value)
        tags = reader.tags(ASSISTANT_NAMESPACE, "context_request")
        requests: list[ContextRequest] = []
        for tag in tags:
            attrs = dict(tag.attrs)
            request_type = attrs.pop("type", "").strip()
            body = tag.body.strip()
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

        return reader.remove(tags), requests

    def _extract_dont_respond(
        self,
        value: str
    ) -> tuple[str, bool]:
        reader = XMLReader(value)
        tags = reader.tags(ASSISTANT_NAMESPACE, "dont_respond")
        return reader.remove(tags), bool(tags)

    def _format_persistent_memory_contexts(self, memories: list[PersistentMemory]) -> list[str]:
        if self.memory_char_budget <= 0:
            return []
        blocks: list[str] = []
        total = 0
        for memory in memories:
            if memory.id is None or not memory.content.strip():
                continue
            block = self._persistent_memory_context_block(memory.id, memory.content)
            block_len = len(block)
            if total + block_len > self.memory_char_budget:
                break
            blocks.append(block)
            total += block_len
        remaining = max(0, self.memory_char_budget - total)
        blocks.append(
            f"<remaining_persistent_memory_chars>{remaining}<remaining_persistent_memory_chars/>"
        )
        return blocks

    def _persistent_memory_context_block(self, memory_id: int, content: str) -> str:
        attrs = f'id={json.dumps(str(memory_id), ensure_ascii=False)}'
        return (
            f"{open_system_tag('persistent_memory').replace('>', f' {attrs}>')}\n"
            f"{content.strip()}\n"
            f"{close_system_tag('persistent_memory')}"
        )

    def _persistent_memory_projected_chars(
        self,
        *,
        edit_id: int | None = None,
        edit_content: str | None = None,
        create_id: int | None = None,
        create_content: str | None = None,
    ) -> int:
        total = 0
        for memory in self.context.persistent_memories():
            if memory.id is None:
                continue
            content = edit_content if memory.id == edit_id and edit_content is not None else memory.content
            if content.strip():
                total += len(self._persistent_memory_context_block(memory.id, content))
        if create_id is not None and create_content is not None and create_content.strip():
            total += len(self._persistent_memory_context_block(create_id, create_content))
        return total

    def _persistent_memory_create_fits(self, content: str) -> tuple[bool, int]:
        memory_id = self.context.next_persistent_memory_id()
        projected = self._persistent_memory_projected_chars(
            create_id=memory_id,
            create_content=content,
        )
        return projected <= self.memory_char_budget, memory_id

    def _persistent_memory_edit_fits(self, memory_id: int, content: str) -> bool:
        projected = self._persistent_memory_projected_chars(
            edit_id=memory_id,
            edit_content=content,
        )
        return projected <= self.memory_char_budget

    def _persistent_memory_assistant_tag(
        self,
        *,
        content: str = "",
        memory_id: int | None = None,
        edit_id: int | None = None,
        remove_id: int | None = None,
        failed: bool = False,
    ) -> str:
        attrs: list[str] = []
        if memory_id is not None:
            attrs.append(f"id={json.dumps(str(memory_id), ensure_ascii=False)}")
        if edit_id is not None:
            attrs.append(f"edit={json.dumps(str(edit_id), ensure_ascii=False)}")
        if remove_id is not None:
            attrs.append(f"remove={json.dumps(str(remove_id), ensure_ascii=False)}")
        if failed:
            attrs.append('failed="true"')
        attr_text = f" {' '.join(attrs)}" if attrs else ""
        if remove_id is not None and not content.strip():
            return f"<{ASSISTANT_NAMESPACE}:persistent_memory{attr_text} />"
        return (
            f"<{ASSISTANT_NAMESPACE}:persistent_memory{attr_text}>"
            f"{content.strip()}"
            f"</{ASSISTANT_NAMESPACE}:persistent_memory>"
        )

    def _extract_text_persistent_memories(self, value: str) -> tuple[str, str]:
        reader = XMLReader(value)
        tags = reader.tags(ASSISTANT_NAMESPACE, "persistent_memory")
        replacements: dict[int, str] = {}
        for tag in tags:
            attrs = dict(tag.attrs)
            body = tag.body.strip()
            edit_id = self._coerce_memory_id(attrs.get("edit"))
            remove_id = self._coerce_memory_id(attrs.get("remove"))
            if remove_id is not None:
                removed = self.context.remove_persistent_memory(remove_id)
                self.logger.debug(f"persistent memory remove id={remove_id} removed={removed}.")
            elif edit_id is not None:
                if not self._persistent_memory_edit_fits(edit_id, body):
                    replacements[tag.start] = self._persistent_memory_assistant_tag(
                        content=body,
                        edit_id=edit_id,
                        failed=True,
                    )
                    self.logger.debug(f"persistent memory edit id={edit_id} failed over budget.")
                    continue
                edited = self.context.edit_persistent_memory(edit_id, body)
                self.logger.debug(f"persistent memory edit id={edit_id} edited={edited is not None}.")
            else:
                fits, _memory_id = self._persistent_memory_create_fits(body)
                if not fits:
                    replacements[tag.start] = self._persistent_memory_assistant_tag(
                        content=body,
                        failed=True,
                    )
                    self.logger.debug("persistent memory create failed over budget.")
                    continue
                created = self.context.create_persistent_memory(body)
                self.logger.debug(f"persistent memory create id={created.id if created is not None else None}.")
                if created is not None and created.id is not None:
                    replacements[tag.start] = self._persistent_memory_assistant_tag(
                        content=created.content,
                        memory_id=created.id,
                    )
        return reader.remove(tags), reader.rewrite(replacements)

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

    def _apply_persistent_memory_tool_call(self, call: _ToolCall) -> dict[str, Any] | None:
        raw_operation = call.arguments.get("operation")
        operation = raw_operation.strip().casefold() if isinstance(raw_operation, str) else ""
        memory_id = self._coerce_memory_id(call.arguments.get("id"))
        raw_content = call.arguments.get("content")
        content = raw_content.strip() if isinstance(raw_content, str) else ""
        if operation == "create":
            fits, _memory_id = self._persistent_memory_create_fits(content)
            if not fits:
                self.logger.debug("persistent memory tool create failed over budget.")
                return {**call.arguments, "failed": True}
            created = self.context.create_persistent_memory(content)
            self.logger.debug(f"persistent memory tool create id={created.id if created is not None else None}.")
            if created is None:
                return None
            return {**call.arguments, "id": created.id}
        if operation == "edit" and memory_id is not None:
            if not self._persistent_memory_edit_fits(memory_id, content):
                self.logger.debug(f"persistent memory tool edit id={memory_id} failed over budget.")
                return {**call.arguments, "failed": True}
            edited = self.context.edit_persistent_memory(memory_id, content)
            self.logger.debug(f"persistent memory tool edit id={memory_id} edited={edited is not None}.")
            return call.arguments if edited is not None else None
        if operation == "remove" and memory_id is not None:
            removed = self.context.remove_persistent_memory(memory_id)
            self.logger.debug(f"persistent memory tool remove id={memory_id} removed={removed}.")
            return call.arguments if removed else None
        self.logger.debug(f"persistent memory tool call ignored: {call.arguments!r}.")
        return None

    def _coerce_memory_id(self, value: Any) -> int | None:
        try:
            memory_id = int(value)
        except (TypeError, ValueError):
            return None
        return memory_id if memory_id > 0 else None

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
    'ai_plugin.BotActionParameters',
    'ai_plugin.BotAction'
]()


def action(
    name: str,
    description: str,
    command_name: str | None = None,
    params: AIParamsTable | None = None,
    **kwargs: 'Unpack[ai_plugin.BotActionParameters]',
) -> 'Callable[[ai_plugin.BotAction], ai_plugin.BotAction]':
    return ai.action(name, description, command_name=command_name, params=params, **kwargs)
