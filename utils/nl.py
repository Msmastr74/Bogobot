from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
from logging import Logger, WARNING, getLogger
import os
import re
import time
import types
from typing import Any, Callable, Generic, Literal, TypeAlias, TypeVar, Union, cast, get_args, get_origin, TYPE_CHECKING
if TYPE_CHECKING:
    from openai import OpenAI
    from openai.types.chat import ChatCompletionToolParam, ChatCompletionMessageToolCallUnion

import discord
import plugins.nl as nl_plugin

getLogger("httpx").setLevel(WARNING)

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")

NLParamsTable: TypeAlias = dict[str, "NLParam"]
_MAX_CALLS = 4
_REQUEST_INTERVAL_SECONDS = 60.0
_ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")

@dataclass(frozen=True, slots=True)
class NLParam:
    description: str | None = None
    type: object = str
    required: bool = True
    default: Any = None


@dataclass(frozen=True, slots=True)
class NLMatch(Generic[ContextT, ActionT]):
    name: str
    command_name: str
    description: str
    context: ContextT
    action: ActionT | None
    score: float
    kwargs: dict[str, Any] | None = None
    reply: str | None = None


@dataclass(frozen=True, slots=True)
class _NLAction(Generic[ContextT, ActionT]):
    name: str
    command_name: str
    tool_name: str
    description: str
    params: dict[str, NLParam]
    context: ContextT
    action: ActionT


@dataclass(frozen=True, slots=True)
class _ToolCall:
    name: str
    arguments: dict[str, Any]


class NLCore(Generic[ContextT, ActionT]):
    def __init__(
        self,
        *,
        enabled: bool = True,
        model_name: str = "llama-3.1-8b-instant",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        logger: Logger | None = None,
    ):
        self.enabled = enabled
        self.model_name = model_name
        self.api_key_env = api_key_env
        self.base_url = base_url
        self._actions: list[_NLAction[ContextT, ActionT]] = []
        self._client: 'OpenAI | None' = None
        self._last_request_at: float | None = None
        self._lock = asyncio.Lock()
        self.logger = logger or getLogger("Bogobot.NL")

    def configure(
        self,
        *,
        enabled: bool | None = None,
        model_name: str | None = None,
        api_key_env: str | None = None,
        base_url: str | None = None,
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

        if logger is not None:
            self.logger = logger

    def action(
        self,
        name: str,
        description: str,
        command_name: str | None = None,
        params: NLParamsTable | None = None,
        **kwargs: Any,
    ) -> Callable[[ActionT], ActionT]:
        def decorator(action: ActionT) -> ActionT:
            normalized_params = self._normalize_params(params or {})
            self._actions.append(_NLAction(
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
    ) -> NLMatch[ContextT, ActionT] | None:
        matches = await self.match_infos(
            text,
            message=message,
            interaction=interaction,
            assistant_context=assistant_context,
        )
        return matches[0] if matches else None

    async def match_infos(
        self,
        text: str,
        *,
        message: discord.Message | None = None,
        interaction: discord.Interaction | None = None,
        assistant_context: str | None = None,
    ) -> list[NLMatch[ContextT, ActionT]]:
        if not self.enabled or not text.strip():
            return []

        async with self._lock:
            client = self._ensure_client()
            await self._wait_for_rate_limit()
            content, calls = await asyncio.to_thread(
                self._complete,
                client,
                text,
                self._actions,
                assistant_context,
            )

        matches: list[NLMatch[ContextT, ActionT]] = []
        if not calls:
            reply = self._coerce_reply(content)
            if reply is None:
                return []
            return [NLMatch(
                name="conversation",
                command_name="conversation",
                description="Conversational NL response",
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
            matches.append(NLMatch(
                name=action.name,
                command_name=action.command_name,
                description=action.description,
                context=action.context,
                action=action.action,
                score=1.0,
                kwargs=kwargs,
            ))

        return matches

    async def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            wait_seconds = _REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait_seconds > 0:
                self.logger.debug(f"rate limit sleeping for {wait_seconds:.2f}s.")
                await asyncio.sleep(wait_seconds)
                now = time.monotonic()
        self._last_request_at = now

    def _ensure_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("The openai package is required when NL is enabled.") from exc

            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                raise RuntimeError(f"NL is enabled but {self.api_key_env} is not set.")
            self._client = OpenAI(api_key=api_key, base_url=self.base_url)
        return self._client

    def _complete(
        self,
        client: 'OpenAI',
        text: str,
        actions: list[_NLAction[ContextT, ActionT]],
        assistant_context: str | None,
    ) -> tuple[str, list[_ToolCall]]:
        assistant_context_text = assistant_context.strip() if assistant_context is not None else ""
        has_assistant_context = bool(assistant_context_text)
        system_prompt = self._system_prompt(actions, has_assistant_context=has_assistant_context)
        tools = [self._tool_schema(action) for action in actions]
        messages: list[Any] = [
            {"role": "system", "content": system_prompt},
        ]
        if has_assistant_context:
            messages.append({
                "role": "assistant",
                "content": f"<|reply_start|>\n{assistant_context_text}\n<|reply_end|>",
            })
            self.logger.debug(f"assistant context: {assistant_context!r}.")
        messages.append({"role": "user", "content": text})
        self.logger.debug(f"input: {text!r}.")
        self.logger.debug(f"system prompt: {system_prompt!r}.")
        self.logger.debug(f"tools: {tools!r}.")
        try:
            response = client.chat.completions.create(
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
        actions: list[_NLAction[ContextT, ActionT]],
        *,
        has_assistant_context: bool,
    ) -> str:
        assistant_context_instruction = (
            "The assistant message immediately before the current user message contains the exact previous message the user replied to, wrapped between <|reply_start|> and <|reply_end|>. "
            "Do not call or invent a command to retrieve it.\n"
            if has_assistant_context else
            ""
        )
        return (
            "Cutting Knowledge Date: December 2023\n"
            f"Today's Date: {datetime.now().strftime('%d %B %Y')}\n"
            f"{nl_plugin.INSTRUCTION_TEXT}\n"
            "The available tools are Discord commands. Refer to them as commands. Use a command when it fits the user's request. You do not have to use commands. Commands only provide output to the user, and end the turn. "
            "Only call commands from the available tools; never invent command names. "
            "If no command fits, respond normally.\n"
            f"{assistant_context_instruction}"
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

    def _tool_schema(self, action: _NLAction[ContextT, ActionT]) -> 'ChatCompletionToolParam':
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

    def _param_schema(self, param: NLParam) -> dict[str, Any]:
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
        action: _NLAction[ContextT, ActionT],
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
        params: NLParamsTable,
    ) -> dict[str, NLParam]:
        normalized: dict[str, NLParam] = {}
        for name, param in params.items():
            if not isinstance(param, NLParam):
                raise TypeError(f"NL parameter {name} must be an NLParam, got {type(param).__name__}")
            if not self._supported_param_type(param.type):
                raise TypeError(f"Unsupported NL parameter type for {name}: {param.type!r}")
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

nl = NLCore[
    nl_plugin.BotActionParameters,
    nl_plugin.BotAction
]()


def action(
    name: str,
    description: str,
    command_name: str | None = None,
    params: NLParamsTable | None = None,
    **kwargs: nl_plugin.BotActionParameters,
) -> Callable[[nl_plugin.BotAction], nl_plugin.BotAction]:
    return nl.action(name, description, command_name=command_name, params=params, **kwargs)
