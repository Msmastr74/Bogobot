import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin

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

DisplayFilterFunc = Callable[[Any, Any], Any]
CacheFilter = DisplayFilterFunc | bool
StatValue = str | int | float | bool


@dataclass(frozen=True)
class DisplayFilter:
    func: DisplayFilterFunc | None = None
    cache: CacheFilter = True


DisplayFilterInput = DisplayFilterFunc | DisplayFilter


@contextmanager
def apply_display_filters(**filters: DisplayFilterInput) -> Iterator[None]:
    yield
    locals_dict = sys._getframe(2).f_locals
    for field_name, display_filter in filters.items():
        field_info = locals_dict.get(field_name)
        if not isinstance(field_info, pydantic.fields.FieldInfo):
            continue
        if not isinstance(display_filter, DisplayFilter):
            display_filter = DisplayFilter(display_filter)
        field_info.metadata.append(display_filter)


def format_count(value: int | float | str | None) -> str:
    if value is None:
        return "Loading..."
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "Loading..."


def format_optional_count(value: int | float | str | None) -> str:
    return format_count(value) if value is not None else "N/A"


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


def format_duration(seconds: int | str | None) -> str:
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


def count_filter(value: Any, _model: Any) -> str:
    return format_count(value)


def rounded_count_filter(value: Any, _model: Any) -> str:
    try:
        return format_count(round(float(value)))
    except (TypeError, ValueError):
        return str(value)


def optional_count_filter(value: Any, _model: Any) -> str:
    return format_optional_count(value)


def bool_filter(value: Any, _model: Any) -> str:
    return format_bool(value)


def duration_filter(value: Any, _model: Any) -> str:
    return format_duration(value)


def best_filter(value: Any, model: Any) -> str:
    return f"{value}/{model.section_count}"


def record_holder_filter(value: Any, model: Any) -> str:
    if value is None:
        return "engine"
    return f"{value.nickname} ({value.value}/{model.section_count})"


def _display_filter(field: pydantic.fields.FieldInfo) -> DisplayFilter | None:
    for item in field.metadata:
        if isinstance(item, DisplayFilter):
            return item
    return None


def _is_model_type(annotation: Any, model_type: type[BaseModel]) -> bool:
    origin = get_origin(annotation)
    if origin is not None:
        return any(_is_model_type(arg, model_type) for arg in get_args(annotation))
    try:
        return issubclass(annotation, model_type)
    except TypeError:
        return False


class StatsGroup(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    def display_fields(self) -> list[tuple[str, str, DisplayFilter]]:
        fields: list[tuple[str, str, DisplayFilter]] = []
        for field_name, field in self.__class__.model_fields.items():
            if field.title is None:
                continue
            display_filter = _display_filter(field) or DisplayFilter()
            fields.append((field_name, field.title, display_filter))
        return fields

    def render_value(self, field_name: str, display_filter: DisplayFilter) -> str | None:
        value = getattr(self, field_name)
        if display_filter.func is not None:
            value = display_filter.func(value, self)
        if value is None:
            return None
        return str(value)

    def rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for field_name, title, display_filter in self.display_fields():
            value = self.render_value(field_name, display_filter)
            if value is not None:
                rows.append((title, value))
        return rows

    def stats_cache(self) -> dict[str, StatValue]:
        cache: dict[str, StatValue] = {}
        for field_name, _title, display_filter in self.display_fields():
            cache_filter = display_filter.cache
            if cache_filter is False:
                continue
            value = getattr(self, field_name)
            if callable(cache_filter):
                value = cache_filter(value, self)
            if isinstance(value, str | int | float):
                cache[field_name] = value
        return cache


class GroupedStatsModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def validate_groups_from_root(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        data = dict(data)
        root_data = dict(data)
        for field_name, field in cls.model_fields.items():
            if _is_model_type(field.annotation, StatsGroup) and not isinstance(data.get(field_name), dict):
                data[field_name] = root_data
        return data

    def groups(self) -> list[tuple[str | None, list[tuple[str, str]]]]:
        groups: list[tuple[str | None, list[tuple[str, str]]]] = []
        for field_name, field in self.__class__.model_fields.items():
            group = getattr(self, field_name)
            if not isinstance(group, StatsGroup):
                continue
            rows = group.rows()
            if rows:
                groups.append((field.title, rows))
        return groups

    def group_dict(self) -> dict[str, dict[str, str]]:
        return {
            title or "Source": dict(rows)
            for title, rows in self.groups()
        }

    def stats_cache(self) -> dict[str, StatValue]:
        cache: dict[str, StatValue] = {}
        for field_name in self.__class__.model_fields:
            group = getattr(self, field_name)
            if isinstance(group, StatsGroup):
                cache.update(group.stats_cache())
        return cache


class StatsSourceGroup(StatsGroup):
    with apply_display_filters(source=DisplayFilter(cache=False)):
        source: str = Field("Bogostream API", title="Source")


class StatsStreamGroup(StatsGroup):
    shuffles: int | float | str = Field("Loading...", title="Shuffles")
    comparisons: int | float | str = Field("Loading...", title="Comparisons")
    best_run: str = Field("Loading...", title="Best Run")
    shuffles_sec: int | float | str = Field("Loading...", title="Shuffles Per Second")
    average_best_shuffle: int | float | str = Field("Loading...", title="Average Best Shuffle")


class StatsApiGroup(StatsGroup):
    engine_total: int | float | str | None = Field(None, title="Engine Total")
    crowd_total: int | float | str | None = Field(None, title="Crowd Total")
    combined_total: int | float | str | None = Field(None, title="Combined Total")
    engine_rate: int | float | str | None = Field(None, title="Engine Rate")
    crowd_rate: int | float | str | None = Field(None, title="Crowd Rate")


class StatsRecentBestGroup(StatsGroup):
    best_at: int | float | str | None = Field(None, title="Best At")
    tick_best: str | None = Field(None, title="Tick Best")
    tick_best_source: str | None = Field(None, title="Tick Best Source")


class StatsContributorsGroup(StatsGroup):
    active_contributors: int | float | str | None = Field(None, title="Active Contributors")
    record_holder: str | None = Field(None, title="Record Holder")
    with apply_display_filters(
        contributions_open=bool_filter,
        solve_confirmed=bool_filter,
    ):
        contributions_open: bool | str | None = Field(None, title="Contributions Open")
        solve_confirmed: bool | str | None = Field(None, title="Solve Confirmed")


class StatsTimingGroup(StatsGroup):
    with apply_display_filters(uptime=duration_filter):
        uptime: int | str = Field("Loading...", title="Uptime [STREAM]")
    with apply_display_filters(elapsed_time=DisplayFilter(cache=False)):
        elapsed_time: str = Field(title="Elapsed Time [STATIC]")


class CachedStatsDisplay(GroupedStatsModel):
    source: StatsSourceGroup = Field(title=None)
    stream: StatsStreamGroup = Field(title="Stream")
    bogostream_api: StatsApiGroup = Field(title="Bogostream API")
    recent_best: StatsRecentBestGroup = Field(title="Recent Best")
    contributors: StatsContributorsGroup = Field(title="Contributors")
    timing: StatsTimingGroup = Field(title="Timing")


class BogostreamRecordHolder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    nickname: str = "unknown"
    value: int = 0


class ApiStatsStreamGroup(StatsGroup):
    section_count: int = Field(25)
    engine_total: int = Field(validation_alias=AliasPath("engine", "total"))
    crowd_total: int = Field(validation_alias=AliasPath("crowd", "total_shuffles"))

    with apply_display_filters(
        shuffles=count_filter,
        best_run=DisplayFilter(best_filter, cache=best_filter),
        shuffles_sec=rounded_count_filter,
    ):
        shuffles: int | None = Field(None, title="Shuffles", validation_alias=AliasPath("combined_total"))
        best_run: int = Field(0, title="Best Run", validation_alias=AliasChoices("record", AliasPath("engine", "best")))
        shuffles_sec: float = Field(title="Shuffles Per Second", validation_alias=AliasPath("combined_rate"))
    comparisons: str = Field("N/A", title="Comparisons")
    average_best_shuffle: str = Field("N/A", title="Average Best Shuffle")


class ApiStatsApiGroup(StatsGroup):
    with apply_display_filters(
        engine_total=count_filter,
        crowd_total=count_filter,
        combined_total=count_filter,
        engine_rate=rounded_count_filter,
        crowd_rate=rounded_count_filter,
    ):
        engine_total: int = Field(title="Engine Total", validation_alias=AliasPath("engine", "total"))
        crowd_total: int = Field(title="Crowd Total", validation_alias=AliasPath("crowd", "total_shuffles"))
        combined_total: int | None = Field(None, title="Combined Total", validation_alias=AliasPath("combined_total"))
        engine_rate: float = Field(title="Engine Rate", validation_alias=AliasPath("engine", "rate"))
        crowd_rate: float = Field(title="Crowd Rate", validation_alias=AliasPath("crowd", "rate"))


class ApiStatsRecentBestGroup(StatsGroup):
    section_count: int = Field(25)

    with apply_display_filters(
        best_at=optional_count_filter,
        tick_best=DisplayFilter(best_filter, cache=best_filter),
    ):
        best_at: int | None = Field(None, title="Best At", validation_alias=AliasPath("engine", "best_at"))
        tick_best: int = Field(title="Tick Best", validation_alias=AliasPath("combined_tick", "best"))
    tick_best_source: str = Field("vps", title="Tick Best Source", validation_alias=AliasPath("combined_tick", "source"))


class ApiStatsContributorsGroup(StatsGroup):
    section_count: int = Field(25)

    with apply_display_filters(
        active_contributors=count_filter,
        record_holder=record_holder_filter,
        contributions_open=bool_filter,
        solve_confirmed=bool_filter,
    ):
        active_contributors: int = Field(0, title="Active Contributors", validation_alias=AliasPath("crowd", "active"))
        record_holder: BogostreamRecordHolder | None = Field(None, title="Record Holder")
        contributions_open: bool | None = Field(None, title="Contributions Open")
        solve_confirmed: bool | None = Field(None, title="Solve Confirmed")


class ApiStatsTimingGroup(StatsGroup):
    with apply_display_filters(uptime=duration_filter):
        uptime: int | None = Field(None, title="Uptime [STREAM]", validation_alias=AliasPath("engine", "uptime_s"))
    with apply_display_filters(elapsed_time=DisplayFilter(cache=False)):
        elapsed_time: str = Field("", title="Elapsed Time [STATIC]")


class BogostreamApiStats(GroupedStatsModel):
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


def cached_stats_display(
    stats: dict[str, StatValue],
    *,
    source: str,
    elapsed_time: str,
) -> CachedStatsDisplay:
    return CachedStatsDisplay.model_validate({
        **stats,
        "source": source,
        "elapsed_time": elapsed_time,
    })


def stats_display_rows(model: GroupedStatsModel) -> list[tuple[str | None, list[tuple[str, str]]]]:
    return model.groups()


def stats_display_dict(model: GroupedStatsModel) -> dict[str, dict[str, str]]:
    return model.group_dict()
