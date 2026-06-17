from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import discord

from modlog.models import ModlogEvent


UndoCriteria = Callable[[discord.Guild, ModlogEvent], Awaitable[Any]]
UndoExec = Callable[[discord.Guild, ModlogEvent, Any], Awaitable[Any]]
UndoDescription = str | Callable[[ModlogEvent, Any], str]
RelatedMatch = Callable[[ModlogEvent, ModlogEvent], bool]
RelatedLimit = Callable[[ModlogEvent], int | None]


@dataclass(frozen=True)
class UndoRule:
    criteria_fn: UndoCriteria
    exec_fn: UndoExec
    description: UndoDescription = "Undo this event."


@dataclass(frozen=True)
class RelatedRule:
    candidate_actions: frozenset[str]
    window_seconds: int
    matches: RelatedMatch
    max_related: RelatedLimit = lambda _event: None


@dataclass(frozen=True)
class ModlogAction:
    name: str
    description: str | None = None
    undo: UndoRule | None = None
    related: tuple[RelatedRule, ...] = ()


class ModlogActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ModlogAction] = {}

    def register(self, action: ModlogAction) -> ModlogAction:
        existing = self._actions.get(action.name)
        if existing is not None:
            action = ModlogAction(
                name=action.name,
                description=action.description if action.description is not None else existing.description,
                undo=action.undo if action.undo is not None else existing.undo,
                related=action.related if action.related else existing.related,
            )
        self._actions[action.name] = action
        return action

    def get(self, name: str) -> ModlogAction | None:
        return self._actions.get(name)

    def values(self) -> Iterable[ModlogAction]:
        return self._actions.values()


ACTIONS = ModlogActionRegistry()


def register(action: ModlogAction) -> ModlogAction:
    return ACTIONS.register(action)
