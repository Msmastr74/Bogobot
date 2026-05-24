from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from logging import Logger, getLogger, WARNING
import re
import types
from typing import Any, Callable, Generic, Literal, TypeVar, Union, cast, get_args, get_origin

import discord
from plugins.nl import BotAction, BotActionParameters
getLogger('httpx').setLevel(WARNING)

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")

NLParamsTable = dict[str, str | tuple[str, object] | tuple[str, object, bool]]
QuantizationMode = Literal["none", "fp16", "4bit", "8bit"]
_MAX_DSL_COMMANDS = 4
_REPLY_ACTION_NAME = "reply"
_ANNOTATED_DISCORD_REFERENCE_RE = re.compile(r"<(@!?|@&|#)([0-9]{15,20}) \"(?:\\.|[^\"\\])*\">")

INSTRUCTION_TEXT = (
    "You are Bogobot, a helpful, friendly, and slightly chaotic Discord bot. "
    "You have a unique persona and love to assist users with their requests while being entertaining."
)

@dataclass(frozen=True, slots=True)
class NLParam:
    description: str
    type: object
    required: bool


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
    description: str
    params: dict[str, NLParam]
    context: ContextT
    action: ActionT


class NLCore(Generic[ContextT, ActionT]):
    def __init__(
        self,
        *,
        enabled: bool = True,
        ranker_model_name: str = "cross-encoder/ms-marco-MiniLM-L6-v2",
        function_model_name: str = "HuggingFaceTB/SmolLM2-360M-Instruct",
        quantization: QuantizationMode = "4bit",
        threshold: float = 0.0,
        top_k: int = 2,
        logger: Logger | None = None,
    ):
        self.enabled = enabled
        self.ranker_model_name = ranker_model_name
        self.function_model_name = function_model_name
        self.quantization = quantization
        self.threshold = threshold
        self.top_k = top_k
        self._actions: list[_NLAction[ContextT, ActionT]] = []
        self._ranker: Any = None
        self._function_tokenizer: Any = None
        self._function_model: Any = None
        self._function_device: str = "cpu"
        self._lock = asyncio.Lock()
        self.logger = logger or getLogger("Bogobot.NL")

    def configure(
        self,
        *,
        enabled: bool | None = None,
        ranker_model_name: str | None = None,
        function_model_name: str | None = None,
        quantization: str | None = None,
        threshold: float | None = None,
        top_k: int | None = None,
        logger: Logger | None = None,
        # Back-compat with the old GLiNER config name.
        model_name: str | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = enabled

        ranker_model_name = ranker_model_name or model_name
        if ranker_model_name is not None and ranker_model_name != self.ranker_model_name:
            self.ranker_model_name = ranker_model_name
            self._ranker = None

        if function_model_name is not None and function_model_name != self.function_model_name:
            self.function_model_name = function_model_name
            self._function_tokenizer = None
            self._function_model = None

        if quantization is not None and quantization != self.quantization:
            if quantization not in ("none", "fp16", "4bit", "8bit"):
                raise ValueError(f"Unsupported NL quantization mode: {quantization}")
            self.quantization = cast(QuantizationMode, quantization)
            self._function_tokenizer = None
            self._function_model = None

        if threshold is not None:
            self.threshold = threshold

        if top_k is not None:
            self.top_k = top_k

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
            self._actions.append(_NLAction(
                name=name,
                command_name=command_name or name,
                description=description,
                params=self._normalize_params(params or {}),
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
    ) -> NLMatch[ContextT, ActionT] | None:
        matches = await self.match_infos(text, message=message)
        return matches[0] if matches else None

    async def match_infos(
        self,
        text: str,
        *,
        message: discord.Message | None = None,
    ) -> list[NLMatch[ContextT, ActionT]]:
        if not self.enabled or not text.strip():
            return []

        async with self._lock:
            candidates: list[tuple[_NLAction[ContextT, ActionT], float]] = []
            if self._actions:
                ranker = await self._ensure_ranker()
                candidates = await asyncio.to_thread(self._rank_actions, ranker, text)

            tokenizer, model = await self._ensure_function_model()
            dsl = await asyncio.to_thread(
                self._decide_dsl,
                tokenizer,
                model,
                text,
                self._dsl_candidates([action for action, _score in candidates]),
            )

        action_by_name = {action.name: (action, score) for action, score in candidates}
        matches: list[NLMatch[ContextT, ActionT]] = []
        for command in self._parse_dsl_commands(dsl):
            tool_name = command.get("name")
            if not isinstance(tool_name, str):
                continue

            if tool_name == _REPLY_ACTION_NAME:
                raw_args = command.get("arguments", {})
                raw_reply = raw_args.get("message", "") if isinstance(raw_args, dict) else ""
                reply = str(raw_reply).strip()
                if reply:
                    matches.append(NLMatch(
                        name="conversation",
                        command_name="conversation",
                        description="Conversational NL response",
                        context=cast(ContextT, {}),
                        action=None,
                        score=1.0,
                        reply=reply,
                    ))
                continue

            action_score = action_by_name.get(tool_name)
            if action_score is None:
                self.logger.debug(f"NL DSL command rejected unknown action {tool_name!r}.")
                continue

            action, score = action_score
            raw_args = command.get("arguments", {})
            kwargs = self._coerce_arguments(
                action,
                raw_args if isinstance(raw_args, dict) else {},
                message=message,
            )
            if kwargs is None:
                self.logger.debug(f"NL DSL command {tool_name} rejected because arguments did not validate: {raw_args!r}.")
                continue

            self.logger.debug(f"NL match succeeded for {text} with score {score} and action {action.name}.")
            matches.append(NLMatch(
                name=action.name,
                command_name=action.command_name,
                description=action.description,
                context=action.context,
                action=action.action,
                score=score,
                kwargs=kwargs,
            ))

        return matches

    async def _ensure_ranker(self):
        if self._ranker is None:
            self._ranker = await asyncio.to_thread(self._load_ranker)
        return self._ranker

    def _load_ranker(self):
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("The transformers and torch packages are required when NL is enabled.") from exc

        tokenizer = AutoTokenizer.from_pretrained(self.ranker_model_name)
        model = AutoModelForSequenceClassification.from_pretrained(self.ranker_model_name)
        device = "cuda" if bool(torch.cuda.is_available()) else "cpu"
        model = model.to(device)
        model.eval()
        return tokenizer, model, device

    async def _ensure_function_model(self):
        if self._function_model is None or self._function_tokenizer is None:
            self._function_tokenizer, self._function_model = await asyncio.to_thread(self._load_function_model)
        return self._function_tokenizer, self._function_model

    def _load_function_model(self):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("The transformers and torch packages are required when NL is enabled.") from exc

        tokenizer = AutoTokenizer.from_pretrained(self.function_model_name)
        cuda = bool(torch.cuda.is_available())
        self._function_device = "cuda" if cuda else "cpu"

        kwargs: dict[str, Any] = {}
        if self.quantization in ("4bit", "8bit"):
            from transformers import BitsAndBytesConfig
            kwargs["device_map"] = "auto"
            
            if self.quantization == "4bit":
                kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype=getattr(torch, "float16"),
                    bnb_4bit_use_double_quant=True,
                )
            else:
                kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_8bit=True,
                )
        else:
            kwargs["dtype"] = (
                getattr(torch, "float16")
                if cuda and self.quantization in ("fp16", "none")
                else getattr(torch, "float32")
            )

        model = AutoModelForCausalLM.from_pretrained(self.function_model_name, **kwargs)
        if self.quantization not in ("4bit", "8bit"):
            model = cast(Any, model).to(self._function_device)
        model.eval()
        return tokenizer, model

    def _rank_actions(
        self,
        ranker: tuple[Any, Any, str],
        text: str,
    ) -> list[tuple[_NLAction[ContextT, ActionT], float]]:
        import torch

        tokenizer, model, device = ranker
        documents = [self._rank_document(action) for action in self._actions]
        inputs = tokenizer(
            [text] * len(documents),
            documents,
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        inputs = {key: value.to(device) for key, value in inputs.items()}
        with torch.no_grad():
            logits = model(**inputs).logits

        if logits.shape[-1] == 1:
            scores = logits.squeeze(-1)
        else:
            scores = logits[:, -1]

        score_values = [float(score) for score in scores.detach().cpu().tolist()]
        ranked = sorted(
            zip(self._actions, score_values),
            key=lambda item: item[1],
            reverse=True,
        )
        self.logger.debug(f"Raw NL ranker actions for {text}: {[(a.name, s) for a, s in ranked]}.")
        candidates = [
            (action, score)
            for action, score in ranked[:max(1, self.top_k)]
            if score >= self.threshold
        ]
        self.logger.debug(f"NL ranker candidates for {text}: {[(a.name, s) for a, s in candidates]}.")
        return candidates

    def _rank_document(self, action: _NLAction[ContextT, ActionT]) -> str:
        params = "\n".join(
            f"- {name}: {self._json_type(param.type)}. {param.description}"
            for name, param in action.params.items()
        )
        return f"Tool: {action.name}\nDescription: {action.description}\nParameters:\n{params or 'none'}"

    def _decide_dsl(
        self,
        tokenizer: Any,
        model: Any,
        text: str,
        candidates: list[_NLAction[ContextT, ActionT]],
    ) -> str:
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList

        prompt = self._dsl_prompt(tokenizer, text, candidates)
        inputs = tokenizer(prompt, return_tensors="pt")
        inputs = {key: value.to(getattr(model, "device", self._function_device)) for key, value in inputs.items()}
        prompt_len = int(inputs["input_ids"].shape[1])
        allows_multiple = self._allows_multiple_dsl_commands(text)
        max_lines = _MAX_DSL_COMMANDS
        min_lines = 2 if allows_multiple else 1
        prefix_allowed_tokens_fn = self._dsl_prefix_allowed_tokens_fn(
            tokenizer,
            prompt_len,
            candidates,
            max_lines=max_lines,
        )
        self_outer = self

        class DSLStoppingCriteria(StoppingCriteria):
            def __call__(
                self,
                input_ids: Any,
                scores: Any,
                **kwargs: Any,
            ) -> Any:
                generated = tokenizer.decode(
                    input_ids[0][prompt_len:].tolist(),
                    skip_special_tokens=True,
                    clean_up_tokenization_spaces=False,
                )
                lines = [line.strip() for line in generated.strip().splitlines() if line.strip()]
                return len(lines) >= min_lines and self_outer._dsl_text_complete(
                    generated,
                    candidates,
                    max_lines=max_lines,
                )

        with torch.no_grad():
            outputs = cast(Any, model).generate(
                **inputs,
                max_new_tokens=1024,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
                repetition_penalty=1.2,
                prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
                stopping_criteria=StoppingCriteriaList([DSLStoppingCriteria()]),
            )
        response = tokenizer.decode(outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        self.logger.debug(f"NL DSL raw response: {response!r}.")
        return response

    def _dsl_prompt(
        self,
        tokenizer: Any,
        text: str,
        candidates: list[_NLAction[ContextT, ActionT]],
    ) -> str:
        actions = "\n".join(self._dsl_action_spec(action) for action in candidates)
        system = (
            f"{INSTRUCTION_TEXT}\n"
            "Your task is to select appropriate actions based on the user's message and output them in the specified DSL. "
            "If no other appropriate actions exist, use the reply action to answer or respond to their message. Do not repeat the user's message or say 'I understand your request.'. Do not pick actions that have no relation to the user's message.\n"
            "Output only DSL lines. "
            "Each DSL line starts with a JSON-quoted action name, followed by optional name=value arguments.\n"
            "Output exactly one DSL line by default. Output multiple DSL lines only when the user explicitly asks "
            "for multiple actions.\n"
            "Use only the listed action names and parameter names. Argument values must be actual values to use.\n\n"
            f"Available actions:\n{actions}"
        )
        user = text
        if getattr(tokenizer, "chat_template", None):
            return cast(Any, tokenizer).apply_chat_template(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tokenize=False,
                add_generation_prompt=True,
            )
        return f"System: {system}\n\nUser: {user}\nAssistant:"

    def _dsl_candidates(
        self,
        candidates: list[_NLAction[ContextT, ActionT]],
    ) -> list[_NLAction[ContextT, ActionT]]:
        return [
            *candidates,
            _NLAction(
                name=_REPLY_ACTION_NAME,
                command_name=_REPLY_ACTION_NAME,
                description=(
                    "Reply conversationally when the user is talking casually, asking something that is not "
                    "a bot command, or when no listed bot command should be called."
                ),
                params={
                    "message": NLParam(
                        description="Non-empty response text to send to the user.",
                        type=str,
                        required=True,
                    ),
                },
                context=cast(ContextT, {}),
                action=cast(ActionT, None),
            ),
        ]

    def _dsl_action_spec(self, action: _NLAction[ContextT, ActionT]) -> str:
        if action.name == _REPLY_ACTION_NAME:
            return (
                f"- {json.dumps(action.name, ensure_ascii=False)}: {action.description}. "
                "Params: message (required string): the actual helpful response or answer to send back to the user."
            )
        if not action.params:
            return f"- {json.dumps(action.name, ensure_ascii=False)}: {action.description}. Params: none."
        params = ", ".join(
            f"{name} ({'required' if param.required and not self._allows_none(param.type) else 'optional'} {self._json_type(param.type)}): {param.description}"
            for name, param in action.params.items()
        )
        return f"- {json.dumps(action.name, ensure_ascii=False)}: {action.description}. Params: {params}."

    def _json_schema(self, param: NLParam) -> dict[str, Any]:
        choices = self._literal_choices(param.type)
        if choices is not None:
            return {"type": "string", "enum": choices, "description": param.description}
        return {"type": self._json_type(param.type), "description": param.description}

    def _json_type(self, annotation: object) -> str:
        target = self._non_none_type(annotation)
        if target is int:
            return "integer"
        if target is float:
            return "number"
        if target is bool:
            return "boolean"
        if self._is_discord_user_type(target):
            return "string"
        return "string"

    def _parse_dsl_commands(self, text: str) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        for line in (line.strip() for line in text.strip().splitlines()):
            if not line:
                continue
            command = self._parse_dsl_call(line)
            if command is not None:
                commands.append(command)
            if len(commands) >= _MAX_DSL_COMMANDS:
                break
        return commands

    def _parse_dsl_call(self, text: str) -> dict[str, Any] | None:
        decoded = self._parse_json_prefix(text)
        if decoded is None:
            return None
        action_name, end = decoded
        if not isinstance(action_name, str):
            return None

        arguments: dict[str, Any] = {}
        rest = text[end:].strip()
        while rest:
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", rest)
            if match is None:
                return None
            param_name = match[1]
            value, rest = self._parse_dsl_value(rest[match.end():])
            arguments[param_name] = value
            rest = rest.strip()
        return {"name": action_name, "arguments": arguments}

    def _parse_dsl_value(self, text: str) -> tuple[Any, str]:
        text = text.lstrip()
        if text.startswith('"'):
            decoded = self._parse_json_prefix(text)
            if decoded is None:
                return "", ""
            value, end = decoded
            if isinstance(value, str):
                value = self._strip_discord_reference_annotations(value)
            return value, text[end:]

        match = re.match(r"\S+", text)
        if match is None:
            return "", ""
        raw = match[0]
        rest = text[match.end():]
        if raw == "null":
            return None, rest
        if raw == "true":
            return True, rest
        if raw == "false":
            return False, rest
        if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", raw):
            return int(raw.replace(",", "")), rest
        try:
            if "." in raw:
                return float(raw.replace(",", "")), rest
        except ValueError:
            pass
        return raw, rest

    def _parse_json_string_value(self, text: str) -> str | None:
        decoded = self._parse_json_prefix(text)
        if decoded is None:
            return None
        value, end = decoded
        if not isinstance(value, str) or text[end:].strip():
            return None
        return value

    def _parse_json_prefix(self, text: str) -> tuple[Any, int] | None:
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return None
        return value, end

    def _strip_discord_reference_annotations(self, text: str) -> str:
        return _ANNOTATED_DISCORD_REFERENCE_RE.sub(r"<\1\2>", text)

    def _dsl_prefix_allowed_tokens_fn(
        self,
        tokenizer: Any,
        prompt_len: int,
        candidates: list[_NLAction[ContextT, ActionT]],
        *,
        max_lines: int,
    ) -> Callable[[int, Any], list[int]]:
        eos_token_id = tokenizer.eos_token_id
        token_pieces: list[tuple[int, str]] = []
        special_ids = set(getattr(tokenizer, "all_special_ids", []) or [])
        for token_id in range(len(tokenizer)):
            if token_id in special_ids:
                continue
            piece = tokenizer.decode(
                [token_id],
                skip_special_tokens=False,
                clean_up_tokenization_spaces=False,
            )
            if piece:
                token_pieces.append((token_id, piece))

        def allowed(_batch_id: int, input_ids: Any) -> list[int]:
            generated_ids = input_ids[prompt_len:].tolist()
            generated = tokenizer.decode(
                generated_ids,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            cached = allowed_cache.get(generated)
            if cached is not None:
                return cached

            allowed_ids: list[int] = []
            if eos_token_id is not None and self._dsl_text_complete(generated, candidates, max_lines=max_lines):
                allowed_ids.append(int(eos_token_id))

            for token_id, piece in token_pieces:
                if self._dsl_prefix_valid(generated + piece, candidates, max_lines=max_lines):
                    allowed_ids.append(token_id)

            if allowed_ids:
                allowed_cache[generated] = allowed_ids
                return allowed_ids
            fallback = [int(eos_token_id)] if eos_token_id is not None else []
            allowed_cache[generated] = fallback
            return fallback

        allowed_cache: dict[str, list[int]] = {}
        return allowed

    def _dsl_prefix_valid(
        self,
        text: str,
        candidates: list[_NLAction[ContextT, ActionT]],
        *,
        max_lines: int = _MAX_DSL_COMMANDS,
    ) -> bool:
        if not text:
            return True
        lines = text.split("\n")
        if len(lines) > max_lines:
            return False

        for line in lines[:-1]:
            if not line or not self._dsl_line_complete(line, candidates):
                return False

        current = lines[-1]
        if not current:
            return True
        return self._dsl_line_prefix_valid(current, candidates)

    def _dsl_text_complete(
        self,
        text: str,
        candidates: list[_NLAction[ContextT, ActionT]],
        *,
        max_lines: int = _MAX_DSL_COMMANDS,
    ) -> bool:
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        return (
            bool(lines) and
            len(lines) <= max_lines and
            all(self._dsl_line_complete(line, candidates) for line in lines)
        )

    def _allows_multiple_dsl_commands(self, text: str) -> bool:
        normalized = f" {text.casefold()} "
        return any(
            marker in normalized
            for marker in (
                "\n",
                ";",
                " and ",
                " and then ",
                " then ",
                " also ",
                " plus ",
            )
        )

    def _dsl_line_prefix_valid(
        self,
        line: str,
        candidates: list[_NLAction[ContextT, ActionT]],
    ) -> bool:
        return self._dsl_call_prefix_valid(line, candidates)

    def _dsl_line_complete(
        self,
        line: str,
        candidates: list[_NLAction[ContextT, ActionT]],
    ) -> bool:
        command = self._parse_dsl_call(line.strip())
        if command is None:
            return False
        action = next((candidate for candidate in candidates if candidate.name == command["name"]), None)
        if action is None:
            return False
        arguments = command.get("arguments", {})
        if not isinstance(arguments, dict):
            return False
        for name, value in arguments.items():
            param = action.params.get(name)
            if param is None:
                return False
            dsl_value = json.dumps(value) if isinstance(value, str) else str(value).lower() if isinstance(value, bool) else "null" if value is None else str(value)
            if not self._dsl_value_complete_valid(dsl_value, param.type):
                return False
        return all(
            name in arguments
            for name, param in action.params.items()
            if param.required and not self._allows_none(param.type)
        )

    def _dsl_call_prefix_valid(
        self,
        text: str,
        candidates: list[_NLAction[ContextT, ActionT]],
    ) -> bool:
        for action in candidates:
            action_literal = json.dumps(action.name, ensure_ascii=False)
            if action_literal.startswith(text):
                return True
            if text.startswith(action_literal):
                rest = text[len(action_literal):]
                if not rest:
                    return True
                if not action.params or not rest.startswith(" "):
                    continue
                if rest == " ":
                    return True
                if rest[1].isspace():
                    continue
                if self._dsl_params_prefix_valid(rest[1:], action):
                    return True
        return False

    def _dsl_params_prefix_valid(
        self,
        text: str,
        action: _NLAction[ContextT, ActionT],
    ) -> bool:
        if not text:
            return True
        parts = self._split_dsl_param_parts(text)
        if parts is None:
            return False
        complete_parts, current = parts
        used: set[str] = set()
        for part in complete_parts:
            name = self._dsl_complete_param_name(part, action)
            if name is None or name in used:
                return False
            used.add(name)
        if not current:
            return True
        return self._dsl_param_prefix_valid(current, action, used)

    def _split_dsl_param_parts(self, text: str) -> tuple[list[str], str] | None:
        parts: list[str] = []
        current = ""
        in_string = False
        escaped = False
        for char in text:
            if in_string:
                current += char
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                current += char
                in_string = True
                continue
            if char.isspace():
                if current:
                    parts.append(current)
                    current = ""
                continue
            current += char
        if in_string:
            return parts, current
        return parts, current

    def _dsl_complete_param_name(
        self,
        part: str,
        action: _NLAction[ContextT, ActionT],
    ) -> str | None:
        if "=" not in part:
            return None
        name, value = part.split("=", 1)
        param = action.params.get(name)
        if param is None:
            return None
        return name if self._dsl_value_complete_valid(value, param.type) else None

    def _dsl_param_prefix_valid(
        self,
        part: str,
        action: _NLAction[ContextT, ActionT],
        used: set[str],
    ) -> bool:
        if "=" not in part:
            return any(name not in used and name.startswith(part) for name in action.params)
        name, value = part.split("=", 1)
        param = action.params.get(name)
        if param is None or name in used:
            return False
        return self._dsl_value_prefix_valid(value, param.type)

    def _dsl_value_prefix_valid(self, value: str, annotation: object) -> bool:
        target = self._non_none_type(annotation)
        if self._allows_none(annotation) and "null".startswith(value):
            return True
        choices = self._literal_choices(annotation)
        if choices is not None:
            return any(json.dumps(choice).startswith(value) for choice in choices)
        if target is bool:
            return "true".startswith(value) or "false".startswith(value)
        if target is int:
            return bool(re.fullmatch(r"[+-]?(?:\d[\d,]*)?", value))
        if target is float:
            return bool(re.fullmatch(r"[+-]?(?:(?:\d[\d,]*)?(?:\.\d*)?)", value))
        if self._is_discord_user_type(target):
            return bool(re.fullmatch(r"(?:<@!?)?\d*", value))
        return self._json_string_prefix_valid(value) and self._json_string_prefix_has_text(value)

    def _dsl_value_complete_valid(self, value: str, annotation: object) -> bool:
        target = self._non_none_type(annotation)
        if self._allows_none(annotation) and value == "null":
            return True
        choices = self._literal_choices(annotation)
        if choices is not None:
            parsed = self._parse_json_string_value(value)
            return parsed in choices
        if target is bool:
            return value in ("true", "false")
        if target is int:
            return bool(re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", value))
        if target is float:
            try:
                float(value.replace(",", ""))
            except ValueError:
                return False
            return bool(re.fullmatch(r"[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d*)?|\.\d+)", value))
        if self._is_discord_user_type(target):
            return bool(re.fullmatch(r"(?:<@!?)?\d+>?", value))
        parsed = self._parse_json_string_value(value)
        return parsed is not None and self._dsl_string_value_valid(parsed)

    def _json_string_prefix_valid(self, text: str) -> bool:
        if not text:
            return True
        if not text.startswith('"'):
            return False

        if len(text) > 1:
            if text[1].isspace():
                return False
            if text[1] == '"':
                return False

        escaped = False
        for index, char in enumerate(text[1:], start=1):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                if index > 1 and text[index - 1].isspace() and text[index - 2] != "\\":
                    return False
                return index == len(text) - 1
        return True

    def _json_string_prefix_has_text(self, text: str) -> bool:
        parsed = self._parse_json_string_value(text)
        if parsed is not None:
            return self._dsl_string_value_valid(parsed)
        if not text.startswith('"'):
            return False

        content = text[1:]
        if content.endswith("\\"):
            content = content[:-1]
        if any(char in content for char in "{}[]"):
            return False
        if content.startswith("#"):
            return False
        if content.startswith("<") and not content.startswith(("<@", "<@!", "<@&", "<#")):
            return False
        return True

    def _dsl_string_value_valid(self, value: str) -> bool:
        if not value:
            return False
        if value[0].isspace() or value[-1].isspace():
            return False
        if any(char in value for char in "{}[]"):
            return False
        if value.startswith("#"):
            return False
        if value.startswith("<") and not value.startswith(("<@", "<@!", "<@&", "<#")):
            return False
        return True

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                value, _end = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    def _coerce_arguments(
        self,
        action: _NLAction[ContextT, ActionT],
        raw_args: dict[str, Any],
        *,
        message: discord.Message | None,
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {}
        for name, param in action.params.items():
            raw_value = raw_args.get(name, _MISSING)
            value = self._coerce_value(param.type, raw_value, message=message)
            if value is _MISSING:
                if param.required and not self._allows_none(param.type):
                    return None
                if name in raw_args or self._allows_none(param.type):
                    kwargs[name] = None
                continue
            kwargs[name] = value
        return kwargs

    def _coerce_value(
        self,
        annotation: object,
        value: Any,
        *,
        message: discord.Message | None,
    ) -> Any:
        target = self._non_none_type(annotation)
        if value is _MISSING or value is None:
            return _MISSING
        if self._is_discord_user_type(target):
            return self._coerce_discord_user(target, value, message=message)
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
        if target in (str, object):
            return self._strip_discord_reference_annotations(str(value))
        return value

    def _coerce_discord_user(
        self,
        annotation: object,
        value: Any,
        *,
        message: discord.Message | None,
    ) -> Any:
        if isinstance(value, (discord.User, discord.Member)):
            return value
        if message is None:
            return _MISSING

        user_id = self._discord_user_id(value)
        if user_id is None:
            return _MISSING

        if message.guild is not None:
            member = message.guild.get_member(user_id)
            if member is not None:
                return member

        for user in message.mentions:
            if user.id == user_id:
                return user

        state = getattr(message, "_state", None)
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
        match = re.fullmatch(r"<@!?([0-9]{15,20})>", value)
        if match is not None:
            return int(match[1])
        if re.fullmatch(r"[0-9]{15,20}", value):
            return int(value)
        return None

    def _normalize_params(
        self,
        params: NLParamsTable,
    ) -> dict[str, NLParam]:
        normalized: dict[str, NLParam] = {}
        for name, value in params.items():
            if isinstance(value, tuple):
                if len(value) == 3:
                    description, param_type, required = value
                else:
                    description, param_type = value
                    required = True
            else:
                description, param_type, required = value, str, True
            if not self._supported_param_type(param_type):
                raise TypeError(f"Unsupported NL parameter type for {name}: {param_type!r}")
            normalized[name] = NLParam(description=description, type=param_type, required=required)
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

nl = NLCore[BotActionParameters, BotAction]()


def action(
    name: str,
    description: str,
    command_name: str | None = None,
    params: NLParamsTable | None = None,
    **kwargs: BotActionParameters,
) -> Callable[[BotAction], BotAction]:
    return nl.action(name, description, command_name=command_name, params=params, **kwargs)
