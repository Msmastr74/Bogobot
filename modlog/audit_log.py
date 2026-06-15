from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterable, Sequence

import discord
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from modlog.database import AuditLogDatabase


class AuditEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class AuditChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    old: Any = None
    new: Any = None
    has_old: bool = False
    has_new: bool = False


class AuditReverseAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    possible: bool
    reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(BaseModel):
    """Self-contained audit log event document.

    `id` is the Discord audit log entry snowflake and should be used as the
    database primary key for imported audit entries.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    guild_id: int
    source: str = "discord_audit_log"
    action: str
    action_value: int | None = None
    category: str | None = None
    created_at: datetime
    imported_at: datetime
    actor: AuditEntity | None = None
    target: AuditEntity | None = None
    reason: str | None = None
    extra: Any = None
    changes: list[AuditChange] = Field(default_factory=list)
    reverse_actions: list[AuditReverseAction] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def actor_id(self) -> int | None:
        return self.actor.id if self.actor is not None else None

    @property
    def target_id(self) -> int | None:
        return self.target.id if self.target is not None else None

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

    events: list[AuditEvent]
    stats: AuditScanStats


def _audit_log_after_id(value: discord.abc.Snowflake | datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return discord.utils.time_snowflake(value, high=False)
    return int(value.id)


def _audit_log_before_id(value: discord.abc.Snowflake | datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return discord.utils.time_snowflake(value, high=True)
    return int(value.id)


def _target_id(entry: discord.AuditLogEntry, target: Any) -> int | None:
    if target is not None and hasattr(target, "id"):
        return int(target.id)
    raw_target_id = getattr(entry, "_target_id", None)
    if raw_target_id is None:
        return None
    return int(raw_target_id)


def _type_name(value: Any) -> str:
    return type(value).__name__


def _entity(value: Any, *, fallback_id: int | None = None) -> AuditEntity | None:
    if value is None and fallback_id is None:
        return None

    value_id = int(value.id) if value is not None and hasattr(value, "id") else fallback_id
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

    return AuditEntity(
        id=value_id,
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

    entity = _entity(value)
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


def _changes(entry: discord.AuditLogEntry) -> list[AuditChange]:
    try:
        old = _diff_dict(entry.before)
        new = _diff_dict(entry.after)
    except Exception:
        return []

    return [
        AuditChange(
            key=key,
            old=old.get(key),
            new=new.get(key),
            has_old=key in old,
            has_new=key in new,
        )
        for key in sorted(set(old) | set(new))
    ]


def _change_map(changes: Iterable[AuditChange]) -> dict[str, AuditChange]:
    return {change.key: change for change in changes}


def _reverse_actions(action: str, target: AuditEntity | None, changes: list[AuditChange]) -> list[AuditReverseAction]:
    target_id = target.id if target is not None else None
    by_key = _change_map(changes)
    reverse_actions: list[AuditReverseAction] = []

    def add(kind: str, possible: bool, reason: str | None = None, **payload: Any) -> None:
        reverse_actions.append(AuditReverseAction(
            kind=kind,
            possible=possible,
            reason=reason,
            payload={key: value for key, value in payload.items() if value is not None},
        ))

    if action == "ban":
        add("member.unban", target_id is not None, target_id=target_id)
    elif action == "unban":
        add("member.ban", target_id is not None, target_id=target_id)
    elif action == "kick":
        add("member.invite_back", False, "kicks cannot be undone directly", target_id=target_id)
    elif action == "member_role_update":
        roles = by_key.get("roles")
        add(
            "member.roles.restore",
            target_id is not None and roles is not None and roles.has_old,
            "old role set unavailable" if roles is None or not roles.has_old else None,
            target_id=target_id,
            roles=roles.old if roles is not None else None,
        )
    elif action == "member_update":
        add("member.restore_fields", target_id is not None and bool(changes), target_id=target_id)
    elif action.endswith("_create"):
        add(f"{action.removesuffix('_create')}.delete", target_id is not None, target_id=target_id)
    elif action.endswith("_delete"):
        add(
            f"{action.removesuffix('_delete')}.recreate",
            target_id is not None and bool(changes),
            "requires enough captured old fields to recreate",
            target_id=target_id,
        )
    elif action.endswith("_update"):
        old_values = {
            change.key: change.old
            for change in changes
            if change.has_old
        }
        add(
            f"{action.removesuffix('_update')}.restore",
            target_id is not None and bool(old_values),
            "old values unavailable" if not old_values else None,
            target_id=target_id,
            old_values=old_values or None,
        )
    elif action in {"message_delete", "message_bulk_delete"}:
        add(
            "message.restore_copy",
            False,
            "audit logs do not include deleted message content",
            target_id=target_id,
        )
    else:
        add("none", False, "no reverse action is defined for this audit log action")

    return reverse_actions


def normalize_entry(entry: discord.AuditLogEntry) -> AuditEvent:
    target = entry.target
    action = entry.action.name
    action_value = entry.action.value
    category = entry.category.name if entry.category is not None else None
    changes = _changes(entry)
    target_entity = _entity(target, fallback_id=_target_id(entry, target))

    return AuditEvent(
        id=int(entry.id),
        guild_id=int(entry.guild.id),
        action=action,
        action_value=action_value if isinstance(action_value, int) else None,
        category=category,
        created_at=entry.created_at,
        imported_at=datetime.now(timezone.utc),
        actor=_entity(entry.user, fallback_id=entry.user_id),
        target=target_entity,
        reason=entry.reason,
        extra=_serialize(entry.extra),
        changes=changes,
        reverse_actions=_reverse_actions(action, target_entity, changes),
        raw={
            "entry": repr(entry),
            "action": repr(entry.action),
            "category": repr(entry.category),
            "target": repr(target),
            "extra": repr(entry.extra),
        },
    )


def scan_entries(entries: Iterable[discord.AuditLogEntry]) -> list[AuditEvent]:
    """Normalize audit log entries without imposing storage/query order."""

    return [normalize_entry(entry) for entry in entries]


def scan_stats(guild_id: int, events: Iterable[AuditEvent]) -> AuditScanStats:
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
    database: "AuditLogDatabase | None" = None,
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
    if database is not None and entries:
        existing_ids = database.query_event_ids(
            guild_id=guild.id,
            action=action.name if action is not None else None,
            actor_id=int(user.id) if user is not None else None,
            after_id=_audit_log_after_id(after),
            before_id=_audit_log_before_id(before),
            limit=None,
        )
        entries = [
            entry
            for entry in entries
            if int(entry.id) not in existing_ids
        ]
    return build_scan(guild.id, entries)


def events_jsonl(events: Iterable[AuditEvent]) -> str:
    return "\n".join(event.to_json() for event in events)


def known_actions() -> tuple[discord.AuditLogAction, ...]:
    return tuple(discord.AuditLogAction)
