from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Iterable, Sequence

import discord
from pydantic import BaseModel, ConfigDict, Field

class ModlogEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    external_id: str | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class ModlogChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    old: Any = None
    new: Any = None
    has_old: bool = False
    has_new: bool = False


class ModlogEvent(BaseModel):
    """Self-contained moderation log event document.

    `id` is the Discord audit log entry snowflake and should be used as the
    database primary key for imported audit entries. Other modlog sources may
    use their own snowflake-style ids.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    guild_id: int
    source: str = "discord_audit_log"
    action: str
    action_value: int | None = None
    category: str | None = None
    imported_at: datetime
    actor: ModlogEntity | None = None
    target: ModlogEntity | None = None
    reason: str | None = None
    extra: Any = None
    changes: list[ModlogChange] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def actor_id(self) -> int | None:
        return self.actor.id if self.actor is not None else None

    @property
    def target_id(self) -> int | None:
        return self.target.id if self.target is not None else None

    @property
    def created_at(self) -> datetime:
        return discord.utils.snowflake_time(self.id)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


class AuditScanStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: int
    scanned: int
    first_id: int | None = None
    last_id: int | None = None
    first_created_at: datetime | None = None
    last_created_at: datetime | None = None


class AuditScan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ModlogEvent]
    stats: AuditScanStats


def _type_name(value: Any) -> str:
    return type(value).__name__


def _entity(value: Any, *, id: int | None) -> ModlogEntity | None:
    if value is None and id is None:
        return None

    raw_id = getattr(value, "id", None)
    external_id = str(raw_id) if raw_id is not None and id is None else None
    data: dict[str, Any] = {}
    if value is not None:
        for attr in (
            "name",
            "display_name",
            "global_name",
            "discriminator",
            "bot",
            "code",
            "url",
        ):
            if not hasattr(value, attr):
                continue
            try:
                attr_value = getattr(value, attr)
            except Exception:
                continue
            if isinstance(attr_value, str | int | float | bool) or attr_value is None:
                data[attr] = attr_value

    return ModlogEntity(
        id=id,
        external_id=external_id,
        type=_type_name(value) if value is not None else "Object",
        data=data,
    )


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, Enum):
        return {
            "type": type(value).__name__,
            "name": value.name,
            "value": value.value,
        }
    if isinstance(value, discord.Permissions):
        return {"type": "Permissions", "value": value.value}
    if isinstance(value, discord.Colour):
        return {"type": "Colour", "value": value.value}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_serialize(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_serialize(item) for item in sorted(value, key=repr)]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}

    value_id = getattr(value, "id", None)
    entity = _entity(value, id=value_id if isinstance(value_id, int) else None)
    if entity is not None and (entity.id is not None or entity.data):
        return entity.model_dump(exclude_none=True)

    if hasattr(value, "__dict__"):
        data: dict[str, Any] = {"type": _type_name(value)}
        for key, item in vars(value).items():
            if key.startswith("_"):
                continue
            data[key] = _serialize(item)
        return data

    return repr(value)


def _diff_dict(diff: Any) -> dict[str, Any]:
    try:
        return {str(key): _serialize(value) for key, value in diff}
    except Exception:
        return {}


def _changes(entry: discord.AuditLogEntry) -> list[ModlogChange]:
    try:
        old = _diff_dict(entry.before)
        new = _diff_dict(entry.after)
    except Exception:
        return []

    return [
        ModlogChange(
            key=key,
            old=old.get(key),
            new=new.get(key),
            has_old=key in old,
            has_new=key in new,
        )
        for key in sorted(set(old) | set(new))
    ]


def normalize_entry(entry: discord.AuditLogEntry) -> ModlogEvent:
    target = entry.target
    action = entry.action.name
    action_value = entry.action.value
    category = entry.category.name if entry.category is not None else None
    changes = _changes(entry)
    target_id = target.id if target is not None and isinstance(target.id, int) else None
    target_entity = _entity(target, id=target_id)

    return ModlogEvent(
        id=int(entry.id),
        guild_id=int(entry.guild.id),
        action=action,
        action_value=action_value if isinstance(action_value, int) else None,
        category=category,
        imported_at=datetime.now(timezone.utc),
        actor=_entity(entry.user, id=entry.user_id),
        target=target_entity,
        reason=entry.reason,
        extra=_serialize(entry.extra),
        changes=changes,
        raw={
            "entry": repr(entry),
            "action": repr(entry.action),
            "category": repr(entry.category),
            "target": repr(target),
            "extra": repr(entry.extra),
        },
    )


def scan_entries(entries: Iterable[discord.AuditLogEntry]) -> list[ModlogEvent]:
    """Normalize audit log entries without imposing storage/query order."""

    return [normalize_entry(entry) for entry in entries]


def scan_stats(guild_id: int, events: Iterable[ModlogEvent]) -> AuditScanStats:
    event_list = sorted(events, key=lambda event: event.id)
    if not event_list:
        return AuditScanStats(guild_id=guild_id, scanned=0)

    return AuditScanStats(
        guild_id=guild_id,
        scanned=len(event_list),
        first_id=event_list[0].id,
        last_id=event_list[-1].id,
        first_created_at=event_list[0].created_at,
        last_created_at=event_list[-1].created_at,
    )


def build_scan(guild_id: int, entries: Iterable[discord.AuditLogEntry]) -> AuditScan:
    events = scan_entries(entries)
    return AuditScan(events=events, stats=scan_stats(guild_id, events))


async def retrieve_entries(
    guild: discord.Guild,
    *,
    limit: int | None = None,
    before: discord.abc.Snowflake | datetime | None = None,
    after: discord.abc.Snowflake | datetime | None = None,
    oldest_first: bool | None = None,
    user: discord.abc.Snowflake | None = None,
    action: discord.AuditLogAction | None = None,
) -> list[discord.AuditLogEntry]:
    """Fetch raw Discord audit log entries without normalizing them."""

    kwargs: dict[str, Any] = {"limit": limit}
    if before is not None:
        kwargs["before"] = before
    if after is not None:
        kwargs["after"] = after
    if oldest_first is not None:
        kwargs["oldest_first"] = oldest_first
    if user is not None:
        kwargs["user"] = user
    if action is not None:
        kwargs["action"] = action

    return [entry async for entry in guild.audit_logs(**kwargs)]


async def iter_entries(
    guild: discord.Guild,
    *,
    limit: int | None = None,
    before: discord.abc.Snowflake | datetime | None = None,
    after: discord.abc.Snowflake | datetime | None = None,
    oldest_first: bool | None = None,
    user: discord.abc.Snowflake | None = None,
    action: discord.AuditLogAction | None = None,
) -> AsyncIterator[discord.AuditLogEntry]:
    """Stream raw Discord audit log entries without normalizing them."""

    kwargs: dict[str, Any] = {"limit": limit}
    if before is not None:
        kwargs["before"] = before
    if after is not None:
        kwargs["after"] = after
    if oldest_first is not None:
        kwargs["oldest_first"] = oldest_first
    if user is not None:
        kwargs["user"] = user
    if action is not None:
        kwargs["action"] = action

    async for entry in guild.audit_logs(**kwargs):
        yield entry


async def retrieve_and_scan(
    guild: discord.Guild,
    *,
    limit: int | None = None,
    before: discord.abc.Snowflake | datetime | None = None,
    after: discord.abc.Snowflake | datetime | None = None,
    oldest_first: bool | None = None,
    user: discord.abc.Snowflake | None = None,
    action: discord.AuditLogAction | None = None,
) -> AuditScan:
    entries = await retrieve_entries(
        guild,
        limit=limit,
        before=before,
        after=after,
        oldest_first=oldest_first,
        user=user,
        action=action,
    )
    return build_scan(guild.id, entries)


def events_jsonl(events: Iterable[ModlogEvent]) -> str:
    return "\n".join(event.to_json() for event in events)


def known_actions() -> tuple[discord.AuditLogAction, ...]:
    return tuple(discord.AuditLogAction)
