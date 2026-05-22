from __future__ import annotations

import asyncio
from dataclasses import dataclass
from logging import Logger, getLogger
from typing import Callable, Generic, Protocol, TypeVar, Any, cast

import numpy as np
from plugins.nl import BotAction, BotActionParameters

ContextT = TypeVar("ContextT")
ActionT = TypeVar("ActionT")


class EmbeddingModel(Protocol):
    def encode(self, sentences: str | list[str]) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class NLMatch(Generic[ContextT, ActionT]):
    name: str
    descriptions: tuple[str, ...]
    context: ContextT
    action: ActionT
    score: float


@dataclass(frozen=True, slots=True)
class _NLAction(Generic[ContextT, ActionT]):
    name: str
    descriptions: tuple[str, ...]
    context: ContextT
    action: ActionT

class NLCore(Generic[ContextT, ActionT]):
    def __init__(
        self,
        *,
        model_name: str = "minishlab/potion-base-32M",
        threshold: float = 0.5,
        logger: Logger | None = None,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self._actions: list[_NLAction[ContextT, ActionT]] = []
        self._model: EmbeddingModel | None = None
        self._description_embeddings: np.ndarray | None = None
        self._description_actions: list[_NLAction[ContextT, ActionT]] = []
        self._dirty = True
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
            self._description_embeddings = None
            self._dirty = True

        if threshold is not None:
            self.threshold = threshold
        
        if logger is not None:
            self.logger = logger

    def action(
        self,
        name: str,
        *descriptions: str,
        **kwargs: Any,
    ) -> Callable[[ActionT], ActionT]:
        def decorator(action: ActionT) -> ActionT:
            action_descriptions = descriptions or (name,)
            self._actions.append(_NLAction(
                name=name,
                descriptions=action_descriptions,
                context=cast(ContextT, kwargs),
                action=action,
            ))
            self._dirty = True
            return action
        return decorator

    async def match(self, text: str) -> ActionT | None:
        match = await self.match_info(text)
        if match is None:
            return None
        return match.action

    async def match_info(self, text: str) -> NLMatch[ContextT, ActionT] | None:
        if not text.strip() or not self._actions:
            return None

        exact_match = self._exact_match(text)
        if exact_match is not None:
            return exact_match

        async with self._lock:
            await self._ensure_embeddings()
            assert self._model is not None
            assert self._description_embeddings is not None

            query_embedding = await asyncio.to_thread(self._model.encode, text)
            query = self._normalize(np.asarray(query_embedding, dtype=np.float32))
            scores = self._description_embeddings @ query
            index = int(np.argmax(scores))
            score = float(scores[index])

            action = self._description_actions[index]
            if score < self.threshold:
                self.logger.debug(
                    f"NL match failed for {text} with score {score}. Closest match was {action.name}."
                )
                return None

            self.logger.debug(
                f"NL match suceeded for {text} with score {score} and action {action.name}."
            )
            return NLMatch(
                name=action.name,
                descriptions=action.descriptions,
                context=action.context,
                action=action.action,
                score=score,
            )

    def _exact_match(self, text: str) -> NLMatch[ContextT, ActionT] | None:
        normalized = self._normalize_text(text)
        for action in self._actions:
            for description in action.descriptions:
                if normalized == self._normalize_text(description):
                    self.logger.debug(f"NL exact match for {text} with action {action.name}.")
                    return NLMatch(
                        name=action.name,
                        descriptions=action.descriptions,
                        context=action.context,
                        action=action.action,
                        score=1.0,
                    )
        return None

    def _normalize_text(self, text: str) -> str:
        return " ".join(text.casefold().split())

    async def _ensure_embeddings(self) -> None:
        if self._model is None:
            self._model = await asyncio.to_thread(self._load_model)

        if not self._dirty and self._description_embeddings is not None:
            return

        descriptions: list[str] = []
        description_actions: list[_NLAction[ContextT, ActionT]] = []
        for action in self._actions:
            for description in action.descriptions:
                descriptions.append(description)
                description_actions.append(action)

        model = self._model
        assert model is not None
        embeddings = await asyncio.to_thread(model.encode, descriptions)
        matrix = np.asarray(embeddings, dtype=np.float32)
        self._description_embeddings = self._normalize_matrix(matrix)
        self._description_actions = description_actions
        self._dirty = False

    def _load_model(self) -> EmbeddingModel:
        try:
            from model2vec import StaticModel  # type: ignore[reportMissingImports]
        except ImportError as exc:
            raise RuntimeError(
                "The model2vec package is required for natural-language actions. "
                "Install project dependencies before using @mention NL matching."
            ) from exc

        return StaticModel.from_pretrained(self.model_name, force_download=False)

    def _normalize(self, vector: np.ndarray) -> np.ndarray:
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return vector
        return vector / norm

    def _normalize_matrix(self, matrix: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-12)

nl = NLCore[BotActionParameters, BotAction]()
def action(
    name: str,
    *descriptions: str,
    **kwargs: BotActionParameters,
) -> Callable[[BotAction], BotAction]:
    return nl.action(name, *descriptions, **kwargs)
