from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import discord

from modlog.audit_log import ModlogEvent


UndoCriteria = Callable[[discord.Guild, ModlogEvent], Awaitable[Any]]
UndoExec = Callable[[discord.Guild, ModlogEvent, Any], Awaitable[Any]]
UndoDescription = str | Callable[[ModlogEvent, Any], str]


@dataclass(frozen=True)
class UndoRule:
    criteria_fn: UndoCriteria
    exec_fn: UndoExec
    description: UndoDescription = "Undo this event."


@dataclass(frozen=True)
class ModlogAction:
    name: str
    name_text: str | None = None
    desc_text: str | None = None
    related_rule: object | None = None
    undo_rule: UndoRule | None = None


class ModlogActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ModlogAction] = {}

    def register(self, action: ModlogAction) -> ModlogAction:
        existing = self._actions.get(action.name)
        if existing is not None:
            action = ModlogAction(
                name=action.name,
                name_text=action.name_text if action.name_text is not None else existing.name_text,
                desc_text=action.desc_text if action.desc_text is not None else existing.desc_text,
                related_rule=action.related_rule if action.related_rule is not None else existing.related_rule,
                undo_rule=action.undo_rule if action.undo_rule is not None else existing.undo_rule,
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
