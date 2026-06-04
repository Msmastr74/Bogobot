import sys
import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar, get_args, get_origin

import pydantic
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

StatValue = str | int | float | bool

T = TypeVar("T", bound='Schema', covariant=True)
T2 = TypeVar("T2", bound='Schema', covariant=True)
@dataclass(frozen=True)
class Filter(Generic[T]):
    display: Callable[[Any, T], Any] | None = None

@dataclass(frozen=True)
class StatsFilter(Filter[T], Generic[T, T2]):
    cache: Callable[[Any, 'T2'], Any] | bool = True

def format_count(value: int | float | str | None) -> str:
    if value is None:
        return "Loading..."
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value if isinstance(value, str) else "Loading..."

def format_bool(value: bool | str | None) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if isinstance(value, str):
        if value.casefold() in ("true", "yes"):
            return "Yes"
        if value.casefold() in ("false", "no"):
            return "No"
        return value
    return "Unknown"

def count_filter(value: int | float | str, _model: 'Schema') -> str | None:
    if value is None:
        return None
    return format_count(value)


def rounded_count_filter(value: int | float | str, _model: 'Schema') -> str | None:
    if value is None:
        return None
    try:
        return format_count(round(float(value)))
    except (TypeError, ValueError):
        return str(value)


def optional_count_filter(value: int | float | str | None, _model: 'Schema') -> str:
    return format_count(value) if value is not None else "N/A"

def bool_filter(value: bool | str | None, _model: 'Schema') -> str | None:
    if value is None:
        return None
    return format_bool(value)


def duration_filter(seconds: int | str | None, _model: 'Schema') -> str:
    if seconds is None:
        return "N/A"
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days:02}:{hours:02}:{minutes:02}:{seconds:02}"

def best_filter(value: Any, model: 'StatsSchemaWithSectionCount') -> str:
    return f"{value}/{model.section_count}"


def record_holder_filter(value: 'BogostreamRecordHolder', model: 'StatsSchemaWithSectionCount') -> str:
    if value is None:
        return "engine"
    return f"{value.nickname} ({value.value}/{model.section_count})"


class Schema(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_by_name=True, validate_by_alias=True, revalidate_instances='subclass-instances')

    @staticmethod
    @contextmanager
    def filter(**filters: Filter) -> Iterator[None]:
        yield
        locals_dict = sys._getframe(2).f_locals
        for field_name, filter_ in filters.items():
            field_info = locals_dict.get(field_name)
            if not isinstance(field_info, pydantic.fields.FieldInfo):
                continue
            field_info.metadata.append(filter_)

    @staticmethod
    def is_model_type(annotation: Any, model_type: type[BaseModel] | None = None) -> bool:
        model_type = Schema if model_type is None else model_type
        origin = get_origin(annotation)
        if origin is not None:
            return any(Schema.is_model_type(arg, model_type) for arg in get_args(annotation))
        try:
            return issubclass(annotation, model_type)
        except TypeError:
            return False

    @staticmethod
    def field_filter(field: pydantic.fields.FieldInfo) -> Filter | None:
        for item in field.metadata:
            if isinstance(item, Filter):
                return item
        return None

    @model_validator(mode="before")
    @classmethod
    def validate_groups_from_root(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        data = dict(data)
        root_data = dict(data)
        for field_name, field in cls.model_fields.items():
            if (
                get_origin(field.annotation) is None
                and Schema.is_model_type(field.annotation)
                and not isinstance(data.get(field_name), Schema)
            ):
                data[field_name] = root_data
        return data

    def display_fields(self) -> list[tuple[str, str, Filter]]:
        fields: list[tuple[str, str, Filter]] = []
        for field_name, field in self.__class__.model_fields.items():
            if field.title is None or self.is_model_type(field.annotation):
                continue
            display_filter = self.field_filter(field) or Filter()
            fields.append((field_name, field.title, display_filter))
        return fields

    def render_value(self, field_name: str, display_filter: Filter) -> str | None:
        value = getattr(self, field_name)
        if display_filter.display is not None:
            value = display_filter.display(value, self)
        if value is None:
            return None
        return str(value)

    def display_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for field_name, title, display_filter in self.display_fields():
            value = self.render_value(field_name, display_filter)
            if value is not None:
                rows.append((title, value))
        return rows

    def groups(self) -> list[tuple[str | None, list[tuple[str, str]]]]:
        groups: list[tuple[str | None, list[tuple[str, str]]]] = []
        for field_name, field in self.__class__.model_fields.items():
            group = getattr(self, field_name)
            if not isinstance(group, Schema):
                continue
            rows = group.display_rows()
            if rows:
                groups.append((field.title, rows))
        return groups


class StatsSchema(Schema):
    def stats_cache(self) -> dict[str, StatValue]:
        cache: dict[str, StatValue] = {}
        for field_name, _title, display_filter in self.display_fields():
            cache_filter = display_filter.cache if isinstance(display_filter, StatsFilter) else True
            if cache_filter is False:
                continue
            value = getattr(self, field_name)
            if callable(cache_filter):
                value = cache_filter(value, self)
            if isinstance(value, str | int | float | bool):
                cache[field_name] = value
        for field_name in self.__class__.model_fields:
            group = getattr(self, field_name)
            if isinstance(group, StatsSchema):
                cache.update(group.stats_cache())
        return cache


class StatsSourceGroup(StatsSchema):
    with StatsSchema.filter(source=StatsFilter(cache=False)):
        source: str = Field("Bogostream API", title="Source")


class StatsStreamGroup(StatsSchema):
    with StatsSchema.filter(
        shuffles=StatsFilter(count_filter),
        shuffles_sec=StatsFilter(rounded_count_filter),
        average_best_shuffle=StatsFilter(rounded_count_filter),
    ):
        shuffles: int | float | str = Field("Loading...", title="Shuffles")
        comparisons: int | float | str = Field("Loading...", title="Comparisons")
        best_run: str = Field("Loading...", title="Best Run")
        shuffles_sec: int | float | str = Field("Loading...", title="Shuffles Per Second")
        average_best_shuffle: int | float | str = Field("Loading...", title="Average Best Shuffle")


class StatsApiGroup(StatsSchema):
    with StatsSchema.filter(
        engine_total=StatsFilter(count_filter),
        crowd_total=StatsFilter(count_filter),
        combined_total=StatsFilter(count_filter),
        engine_rate=StatsFilter(rounded_count_filter),
        crowd_rate=StatsFilter(rounded_count_filter),
    ):
        engine_total: int | float | str | None = Field(None, title="Engine Total")
        crowd_total: int | float | str | None = Field(None, title="Crowd Total")
        combined_total: int | float | str | None = Field(None, title="Combined Total")
        engine_rate: int | float | str | None = Field(None, title="Engine Rate")
        crowd_rate: int | float | str | None = Field(None, title="Crowd Rate")


class StatsRecentBestGroup(StatsSchema):
    with StatsSchema.filter(best_at=StatsFilter(count_filter)):
        best_at: int | float | str | None = Field(None, title="Best At")
    tick_best: str | None = Field(None, title="Tick Best")
    tick_best_source: str | None = Field(None, title="Tick Best Source")


class StatsContributorsGroup(StatsSchema):
    with StatsSchema.filter(active_contributors=StatsFilter(count_filter)):
        active_contributors: int | float | str | None = Field(None, title="Active Contributors")
    record_holder: str | None = Field(None, title="Record Holder")
    with StatsSchema.filter(
        contributions_open=StatsFilter(bool_filter),
        solve_confirmed=StatsFilter(bool_filter),
    ):
        contributions_open: bool | str | None = Field(None, title="Contributions Open")
        solve_confirmed: bool | str | None = Field(None, title="Solve Confirmed")


class StatsTimingGroup(StatsSchema):
    with StatsSchema.filter(uptime=StatsFilter(duration_filter)):
        uptime: int | str = Field("Loading...", title="Uptime [STREAM]")
    with StatsSchema.filter(elapsed_time=StatsFilter(cache=False)):
        elapsed_time: str = Field(title="Elapsed Time [STATIC]")


class CachedStatsDisplay(StatsSchema):
    source: StatsSourceGroup = Field(title=None)
    stream: StatsStreamGroup = Field(title="Stream")
    bogostream_api: StatsApiGroup = Field(title="Bogostream API")
    recent_best: StatsRecentBestGroup = Field(title="Recent Best")
    contributors: StatsContributorsGroup = Field(title="Contributors")
    timing: StatsTimingGroup = Field(title="Timing")


class BogostreamRecordHolder(Schema):
    nickname: str = "unknown"
    value: int = 0

class StatsSchemaWithSectionCount(StatsSchema):
    section_count: int = Field(25)

class ApiStatsStreamGroup(StatsSchemaWithSectionCount):
    engine_total: int = Field(validation_alias=AliasPath("engine", "total"))
    crowd_total: int = Field(validation_alias=AliasPath("crowd", "total_shuffles"))

    with StatsSchema.filter(
        shuffles=StatsFilter(count_filter),
        best_run=StatsFilter(best_filter, cache=best_filter),
        shuffles_sec=StatsFilter(rounded_count_filter),
    ):
        shuffles: int | None = Field(None, title="Shuffles", validation_alias=AliasPath("combined_total"))
        best_run: int = Field(0, title="Best Run", validation_alias=AliasChoices("record", AliasPath("engine", "best")))
        shuffles_sec: float = Field(title="Shuffles Per Second", validation_alias=AliasPath("combined_rate"))
    comparisons: str = Field("N/A", title="Comparisons")
    average_best_shuffle: str = Field("N/A", title="Average Best Shuffle")


class ApiStatsApiGroup(StatsSchema):
    with StatsSchema.filter(
        engine_total=StatsFilter(count_filter),
        crowd_total=StatsFilter(count_filter),
        combined_total=StatsFilter(count_filter),
        engine_rate=StatsFilter(rounded_count_filter),
        crowd_rate=StatsFilter(rounded_count_filter),
    ):
        engine_total: int = Field(title="Engine Total", validation_alias=AliasPath("engine", "total"))
        crowd_total: int = Field(title="Crowd Total", validation_alias=AliasPath("crowd", "total_shuffles"))
        combined_total: int | None = Field(None, title="Combined Total", validation_alias=AliasPath("combined_total"))
        engine_rate: float = Field(title="Engine Rate", validation_alias=AliasPath("engine", "rate"))
        crowd_rate: float = Field(title="Crowd Rate", validation_alias=AliasPath("crowd", "rate"))


class ApiStatsRecentBestGroup(StatsSchemaWithSectionCount):
    with StatsSchema.filter(
        best_at=StatsFilter(optional_count_filter),
        tick_best=StatsFilter(best_filter, cache=best_filter),
    ):
        best_at: int | None = Field(None, title="Best At", validation_alias=AliasPath("engine", "best_at"))
        tick_best: int = Field(title="Tick Best", validation_alias=AliasPath("combined_tick", "best"))
    tick_best_source: str = Field("vps", title="Tick Best Source", validation_alias=AliasPath("combined_tick", "source"))


class ApiStatsContributorsGroup(StatsSchemaWithSectionCount):
    with StatsSchema.filter(
        active_contributors=StatsFilter(count_filter),
        record_holder=StatsFilter(record_holder_filter),
        contributions_open=StatsFilter(bool_filter),
        solve_confirmed=StatsFilter(bool_filter),
    ):
        active_contributors: int = Field(0, title="Active Contributors", validation_alias=AliasPath("crowd", "active"))
        record_holder: BogostreamRecordHolder | None = Field(None, title="Record Holder")
        contributions_open: bool | None = Field(None, title="Contributions Open")
        solve_confirmed: bool | None = Field(None, title="Solve Confirmed")


class ApiStatsTimingGroup(StatsSchema):
    with StatsSchema.filter(uptime=StatsFilter(duration_filter)):
        uptime: int | None = Field(None, title="Uptime [STREAM]", validation_alias=AliasPath("engine", "uptime_s"))
    with StatsSchema.filter(elapsed_time=StatsFilter(cache=False)):
        elapsed_time: str = Field("", title="Elapsed Time [STATIC]")


class BogostreamApiStats(StatsSchema):
    source: StatsSourceGroup = Field(title=None)
    stream: ApiStatsStreamGroup = Field(title="Stream")
    bogostream_api: ApiStatsApiGroup = Field(title="Bogostream API")
    recent_best: ApiStatsRecentBestGroup = Field(title="Recent Best")
    contributors: ApiStatsContributorsGroup = Field(title="Contributors")
    timing: ApiStatsTimingGroup = Field(title="Timing")
    tick_best_arr: list[int] = Field(validation_alias=AliasPath("combined_tick", "best_arr"))

    @model_validator(mode="before")
    @classmethod
    def add_runtime_defaults(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "source" not in data:
            data["source"] = "Bogostream API"
        context = info.context if isinstance(info.context, dict) else {}
        if "section_count" not in data and "section_count" in context:
            data["section_count"] = context["section_count"]
        if "elapsed_time" not in data and "elapsed_time" in context:
            data["elapsed_time"] = context["elapsed_time"]
        return data


class StreamBadge(Schema):
    id: str | None = None
    name: str | None = None
    rarity: str | None = None
    held: bool = False
    edition: int | None = None
    value: int | None = None

    def label(self) -> str:
        return self.name or self.id or "unknown"


class StreamboardEntry(Schema):
    nickname: str = "unknown"
    total: int = 0
    rate: int = 0
    devices: int | None = None
    badges: list[StreamBadge] = Field(default_factory=list)

    def badges_text(self, *, held_only: bool = False) -> str:
        badges = [
            badge
            for badge in self.badges
            if not held_only or badge.held
        ]
        if not badges:
            return "None"
        return ", ".join(badge.label() for badge in badges)


class StreamboardLeaderboard(Schema):
    top: list[StreamboardEntry] = Field(default_factory=list)
    count: int = 0
    sum_all: int | None = None
    view: str = ""
    updated_at: int = 0

    def updated_datetime(self) -> datetime.datetime | None:
        try:
            return datetime.datetime.fromtimestamp(int(self.updated_at) / 1000)
        except (TypeError, ValueError, OSError):
            return None


class StreamContributor(Schema):
    nickname: str
    total: int = 0
    all_time_best: int = 0
    active_ms: int = 0
    max_session_ms: int = 0
    badges: list[StreamBadge] = Field(default_factory=list)
    created_at: int | None = None

    def created_datetime(self) -> datetime.datetime | None:
        if self.created_at is None:
            return None
        try:
            return datetime.datetime.fromtimestamp(int(self.created_at) / 1000)
        except (TypeError, ValueError, OSError):
            return None

    def badges_text(self, *, held_only: bool = False) -> str:
        badges = [
            badge
            for badge in self.badges
            if not held_only or badge.held
        ]
        if not badges:
            return "None"
        return ", ".join(badge.label() for badge in badges)


class SortoffsPlayer(Schema):
    pos: int | str = "?"
    name: str = "Unknown"
    elo: int = 0
    peak_elo: int = 0
    rank: str = ""
    games_played: int = 0
    win_rate: int = 0
    current_streak: int = 0
    max_win_streak: int = 0


class SortoffsLeaderboard(Schema):
    rows: list[SortoffsPlayer] = Field(default_factory=list)
