from __future__ import annotations

import asyncio
from dataclasses import dataclass
from logging import Logger, getLogger
import re
import types
from typing import Callable, Generic, TypeVar, Any, Literal, Union, get_args, get_origin, cast, TYPE_CHECKING
if TYPE_CHECKING:
    from gliner2.inference.engine import GLiNER2

import discord
from plugins.nl import BotAction, BotActionParameters

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")
_NO_ACTION_LABEL = "no_action"
_PARAM_STRUCTURE_PREFIX = "parameters_for"
_PARAM_EXTRACTION_THRESHOLD = 0.01
_INTEGER_PATTERN = r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)"
_FLOAT_PATTERN = r"[+-]?(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\.\d+)"
_NO_ACTION_DESCRIPTION = (
    "no matching bot action: the message is unclear, contains unrelated words, "
    "combines multiple commands, does not ask the bot to do one known action, "
    "or asks for a known action with invalid parameter types such as decimal "
    "numbers for integer-only parameters"
)

@dataclass(frozen=True, slots=True)
class NLMatch(Generic[ContextT, ActionT]):
    name: str
    command_name: str
    description: str
    context: ContextT
    action: ActionT
    score: float
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NLParam:
    description: str
    type: object
    required: bool

NLParamsTable = dict[str, str | tuple[str, object] | tuple[str, object, bool]]

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
        model_name: str = "fastino/gliner2-base-v1",
        threshold: float = 0.5,
        logger: Logger | None = None,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self._actions: list[_NLAction[ContextT, ActionT]] = []
        self._model: 'GLiNER2 | None' = None
        self._lock = asyncio.Lock()
        self.logger = logger or getLogger("Bogobot.NL")

    def configure(
        self,
        *,
        model_name: str | None = None,
        threshold: float | None = None,
        logger: Logger | None = None,
    ) -> None:
        if model_name is not None and model_name != self.model_name:
            self.model_name = model_name
            self._model = None

        if threshold is not None:
            self.threshold = threshold
        
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
        if not text.strip() or not self._actions:
            return None

        async with self._lock:
            model = await self._ensure_model()
            candidates = await asyncio.to_thread(self._classify_actions, model, text)
            if not candidates:
                return None

            action: _NLAction[ContextT, ActionT] | None = None
            score = 0.0
            kwargs: dict[str, Any] | None = None
            for candidate_action, candidate_score in candidates:
                if candidate_score < self.threshold:
                    self.logger.debug(
                        f"NL match failed for {text} with score {candidate_score}. Closest match was {candidate_action.name}."
                    )
                    return None

                candidate_kwargs = await self._extract_kwargs(model, candidate_action, text, message=message)
                if candidate_kwargs is None:
                    self.logger.debug(
                        f"NL candidate {candidate_action.name} rejected for {text} because parameters did not validate."
                    )
                    continue

                action = candidate_action
                score = candidate_score
                kwargs = candidate_kwargs
                break

            if action is None or kwargs is None:
                return None

            self.logger.debug(
                f"NL match suceeded for {text} with score {score} and action {action.name}."
            )
            return NLMatch(
                name=action.name,
                command_name=action.command_name,
                description=action.description,
                context=action.context,
                action=action.action,
                score=score,
                kwargs=kwargs,
            )

    def _normalize_text(self, text: str) -> str:
        return " ".join(text.casefold().split())

    async def _ensure_model(self) -> 'GLiNER2':
        if self._model is None:
            self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> 'GLiNER2':
        try:
            from gliner2.inference.engine import GLiNER2
        except ImportError as exc:
            raise RuntimeError(
                "The gliner2 package is required for natural-language actions. "
                "Install project dependencies before using @mention NL matching."
            ) from exc

        return GLiNER2.from_pretrained(self.model_name)

    def _classify_actions(
        self,
        model: 'GLiNER2',
        text: str,
    ) -> list[tuple[_NLAction[ContextT, ActionT], float]]:
        action_by_label: dict[str, _NLAction[ContextT, ActionT]] = {}
        label_descriptions: dict[str, str] = {}
        for action in self._actions:
            label = self._action_label(action, action_by_label)
            action_by_label[label] = action
            label_descriptions[label] = self._classification_description(action)

        label_descriptions[_NO_ACTION_LABEL] = _NO_ACTION_DESCRIPTION
        result = model.classify_text(
            text,
            {
                "action": {
                    "labels": label_descriptions,
                    "multi_label": True,
                    "cls_threshold": 0.0,
                }
            },
            threshold=self.threshold,
            include_confidence=True,
        )
        classifications = self._classification_results(result)
        if not classifications:
            self.logger.debug(f"NL raw classification result for {text}: {result!r}.")
        self.logger.debug(f"NL classification results for {text}: {classifications}.")

        candidates: list[tuple[_NLAction[ContextT, ActionT], float]] = []
        for label, score in classifications:
            if label == _NO_ACTION_LABEL:
                continue

            action = action_by_label.get(label)
            if action is None:
                normalized_label = self._normalize_text(label)
                for candidate, candidate_action in action_by_label.items():
                    if self._normalize_text(candidate) == normalized_label:
                        action = candidate_action
                        break

            if action is not None:
                candidates.append((action, score))
        return candidates

    def _action_label(
        self,
        action: _NLAction[ContextT, ActionT],
        existing: dict[str, _NLAction[ContextT, ActionT]],
    ) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", action.name.casefold()).strip("_")
        label = normalized or "literal"
        if label == _NO_ACTION_LABEL:
            label = f"{label}_action"

        if label not in existing:
            return label

        suffix = 2
        while f"{label}_{suffix}" in existing:
            suffix += 1
        return f"{label}_{suffix}"

    def _classification_description(self, action: _NLAction[ContextT, ActionT]) -> str:
        if not action.params:
            return action.description

        param_descriptions = []
        for name, param in action.params.items():
            requirement = "required" if param.required else "optional"
            param_descriptions.append(
                f"{name} is a {requirement} {self._type_description(param.type)} parameter: {param.description}"
            )
        return f"{action.description} Parameters: {' '.join(param_descriptions)}"

    def _type_description(self, annotation: object) -> str:
        target_type = self._non_none_type(annotation)
        literal_choices = self._literal_choices(target_type)
        if literal_choices is not None:
            return f"choice of {', '.join(literal_choices)}"
        if target_type is int:
            return "integer, whole-number only, not decimal or floating point"
        if target_type is float:
            return "floating point number"
        if target_type is str:
            return "text"
        if self._is_discord_user_type(target_type):
            return "Discord user or member mention"
        if target_type in (None, type(None)):
            return "empty/null"
        return str(target_type)

    def _classification_result(self, result: Any) -> tuple[str | None, float]:
        results = self._classification_results(result)
        if not results:
            return None, 0.0
        return results[0]

    def _classification_results(self, result: Any) -> list[tuple[str, float]]:
        if isinstance(result, dict):
            value = result.get("action")
            values = self._classification_values(value)
            if values:
                return values
            relation_values = self._relation_classification_values(result.get("relation_extraction"))
            if relation_values:
                return relation_values

            if result:
                scored_items = [
                    (label, score)
                    for label, score in (
                        self._classification_value(value)
                        for value in result.values()
                    )
                    if label is not None
                ]
                if scored_items:
                    return sorted(scored_items, key=lambda item: item[1], reverse=True)

                numeric_items = [
                    (str(label), score)
                    for label, score in result.items()
                    if isinstance(score, (int, float))
                ]
                if numeric_items:
                    return sorted(numeric_items, key=lambda item: item[1], reverse=True)

        if isinstance(result, str):
            return [(result, 1.0)]
        return []

    def _relation_classification_values(self, value: Any) -> list[tuple[str, float]]:
        if not isinstance(value, dict):
            return []
        results: list[tuple[str, float]] = []
        for items in value.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if (
                    isinstance(item, tuple) and
                    len(item) >= 2 and
                    isinstance(item[1], (int, float))
                ):
                    results.append((str(item[0]), float(item[1])))
        return sorted(results, key=lambda item: item[1], reverse=True)

    def _classification_value(self, value: Any) -> tuple[str | None, float]:
        values = self._classification_values(value)
        if not values:
            return None, 0.0
        return values[0]

    def _classification_values(self, value: Any) -> list[tuple[str, float]]:
        if isinstance(value, str):
            return [(value, 1.0)]
        if isinstance(value, tuple) and len(value) >= 2:
            label, score = value[0], value[1]
            if isinstance(score, (int, float)):
                return [(str(label), float(score))]
        if isinstance(value, dict):
            label = value.get("label") or value.get("value") or value.get("text")
            score = value.get("score", value.get("confidence", 1.0))
            if label is not None and isinstance(score, (int, float)):
                return [(str(label), float(score))]

            mapped: list[tuple[str, float]] = []
            for key, item in value.items():
                if isinstance(item, (int, float)):
                    mapped.append((str(key), float(item)))
                    continue
                if isinstance(item, dict):
                    item_score = item.get("score", item.get("confidence"))
                    if isinstance(item_score, (int, float)):
                        mapped.append((str(key), float(item_score)))
            if mapped:
                return sorted(mapped, key=lambda item: item[1], reverse=True)
        if isinstance(value, list) and value:
            results: list[tuple[str, float]] = []
            for item in value:
                results.extend(self._classification_values(item))
            return sorted(results, key=lambda item: item[1], reverse=True)
        return []

    async def _extract_kwargs(
        self,
        model: 'GLiNER2',
        action: _NLAction[ContextT, ActionT],
        text: str,
        *,
        message: discord.Message | None,
    ) -> dict[str, Any] | None:
        if not action.params:
            return {}

        raw_fields: dict[str, Any] = {}
        if any(not self._is_discord_user_type(param.type) for param in action.params.values()):
            result = await asyncio.to_thread(
                self._extract_structured_params,
                model,
                action,
                text,
            )
            self.logger.debug(f"NL raw param extraction result for {action.name} from {text}: {result!r}.")
            raw_fields = self._structure_result(result, action)
            self.logger.debug(f"NL parsed param extraction result for {action.name} from {text}: {raw_fields!r}.")

        kwargs: dict[str, Any] = {}
        for name, param in action.params.items():
            value = self._extract_param_value(
                param,
                raw_fields.get(name, _MISSING),
                message=message,
            )
            if value is _MISSING:
                if not param.required:
                    continue
                if self._allows_none(param.type):
                    kwargs[name] = None
                    continue
                self.logger.debug(
                    f"NL param extraction failed for required param {name}. Raw value was {raw_fields.get(name, _MISSING)!r}."
                )
                return None
            kwargs[name] = value
        return kwargs

    def _extract_structured_params(
        self,
        model: 'GLiNER2',
        action: _NLAction[ContextT, ActionT],
        text: str,
    ) -> Any:
        from gliner2.inference.schema import RegexValidator
        
        schema = model.create_schema()
        structure_name = self._param_structure_name(action)
        structure = schema.structure(structure_name)
        field_labels = self._param_field_labels(action)
        for name, param in action.params.items():
            if self._is_discord_user_type(param.type):
                continue

            target_type = self._non_none_type(param.type)
            validators: list[RegexValidator] = []
            if target_type is int:
                validators.append(RegexValidator(_INTEGER_PATTERN))
            elif target_type is float:
                validators.append(RegexValidator(_FLOAT_PATTERN))

            structure.field(
                field_labels[name],
                dtype=self._schema_dtype(param.type),
                choices=self._literal_choices(param.type),
                description=self._param_field_description(param),
                validators=validators,
            )

        return model.extract(
            text,
            schema,
            threshold=_PARAM_EXTRACTION_THRESHOLD,
            include_confidence=False,
        )

    def _structure_result(
        self,
        result: Any,
        action: _NLAction[ContextT, ActionT],
    ) -> dict[str, Any]:
        params = None
        if isinstance(result, dict):
            params = result.get(self._param_structure_name(action))
            if params is None:
                params = result.get("params")
            if params is None:
                params = result.get("parameters")
        else:
            params = result
        if isinstance(params, list):
            first = params[0] if params else {}
            params = first
        if isinstance(params, dict):
            field_labels = self._param_field_labels(action)
            return {
                name: params[label]
                for name, label in field_labels.items()
                if label in params
            }
        if not isinstance(result, dict):
            return {}
        field_labels = self._param_field_labels(action)
        return {
            name: result[label]
            for name, label in field_labels.items()
            if label in result
        }

    def _param_structure_name(self, action: _NLAction[ContextT, ActionT]) -> str:
        return f"{_PARAM_STRUCTURE_PREFIX}_{self._action_label(action, {})}"

    def _param_field_labels(self, action: _NLAction[ContextT, ActionT]) -> dict[str, str]:
        labels: dict[str, str] = {}
        for name, param in action.params.items():
            if self._is_discord_user_type(param.type):
                continue

            label = self._param_field_label(name, param)
            if label in labels.values():
                suffix = 2
                while f"{label}_{suffix}" in labels.values():
                    suffix += 1
                label = f"{label}_{suffix}"
            labels[name] = label
        return labels

    def _param_field_label(self, name: str, param: NLParam) -> str:
        target_type = self._non_none_type(param.type)
        if name == "max":
            return "maximum_number" if target_type in (int, float) else "maximum_value"
        if name == "min":
            return "minimum_number" if target_type in (int, float) else "minimum_value"

        type_suffix = ""
        if target_type is int:
            type_suffix = "_integer"
        elif target_type is float:
            type_suffix = "_number"

        normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
        return f"{normalized}{type_suffix}" if normalized else f"parameter{type_suffix}"

    def _param_field_description(self, param: NLParam) -> str:
        target_type = self._non_none_type(param.type)
        if target_type is int:
            return f"{param.description} Extract a whole integer number only, such as 5 or 1,000. Do not extract decimal numbers."
        if target_type is float:
            return f"{param.description} Extract a number, including decimals like 0.5, 1.0, or 1,000.25."
        return param.description

    def _extract_param_value(
        self,
        param: NLParam,
        raw_value: Any,
        *,
        message: discord.Message | None,
    ) -> Any:
        target_type = self._non_none_type(param.type)
        if self._is_discord_user_type(target_type):
            return self._extract_user(message)
        if target_type in (None, type(None)):
            return None
        if raw_value is _MISSING or raw_value is None or raw_value == "":
            return _MISSING

        raw = self._raw_param_text(raw_value)
        if raw is None:
            return _MISSING

        literal_choices = self._literal_choices(param.type)
        if literal_choices is not None:
            return raw if raw in literal_choices else _MISSING

        if target_type is int:
            try:
                return int(raw.replace(",", ""))
            except ValueError:
                return _MISSING
        if target_type is float:
            try:
                return float(raw.replace(",", ""))
            except ValueError:
                return _MISSING
        if target_type in (str, object):
            return raw
        return raw

    def _raw_param_text(self, value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, list):
            if not value:
                return None
            return self._raw_param_text(value[0])
        if value is None:
            return None
        return str(value).strip()

    def _extract_user(self, message: discord.Message | None) -> Any:
        if message is None:
            return _MISSING
        state_user = getattr(message._state, "user", None)
        state_user_id = getattr(state_user, "id", None)
        for user in message.mentions:
            if user.id != state_user_id:
                return user
        return _MISSING

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
            normalized[name] = NLParam(
                description=description, type=param_type,
                required=required
            )
        return normalized

    def _supported_param_type(self, annotation: object) -> bool:
        non_none = self._non_none_type(annotation)
        return (
            non_none in (str, int, float, object, None, type(None)) or
            self._literal_choices(non_none) is not None or
            self._is_discord_user_type(non_none)
        )

    def _schema_dtype(self, annotation: object) -> Literal["str", "list"]:
        target_type = self._non_none_type(annotation)
        origin = get_origin(target_type)
        if origin in (list, set, tuple):
            return "list"
        return "str"

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
