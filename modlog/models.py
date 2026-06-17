from datetime import datetime
from typing import Any, Literal

import discord
from pydantic import BaseModel, ConfigDict, Field


Order = Literal["asc", "desc"]


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


class ModlogEventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guild_id: int | None = None
    action: str | None = None
    actor_id: int | None = None
    target_id: int | None = None
    after_id: int | None = None
    before_id: int | None = None
    limit: int | None = Field(default=100, ge=1)
    offset: int = Field(default=0, ge=0)
    order: Order = "desc"
