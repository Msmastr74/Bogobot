from collections.abc import Callable, Iterable
from dataclasses import dataclass

from modlog.audit_log import ModlogEvent
from modlog.database import discord_time_snowflake_offset


DEFAULT_WINDOW_SECONDS = 10
MESSAGE_BULK_WINDOW_SECONDS = 30


@dataclass(frozen=True)
class RelatedRule:
    actions: frozenset[str]
    candidate_actions: frozenset[str]
    window_seconds: int
    matches: Callable[[ModlogEvent, ModlogEvent], bool]
    max_related: Callable[[ModlogEvent], int | None] = lambda _event: None


@dataclass(frozen=True)
class RelatedGroup:
    events: tuple[ModlogEvent, ...]

    @property
    def first_id(self) -> int:
        return self.events[0].id


def event_channel_id(event: ModlogEvent) -> int | None:
    message = event.raw.get("message")
    if isinstance(message, dict):
        channel_id = message.get("channel_id")
        return channel_id if isinstance(channel_id, int) else None

    extra = event.extra
    if isinstance(extra, dict):
        channel = extra.get("channel")
        if isinstance(channel, dict):
            channel_id = channel.get("id")
            return channel_id if isinstance(channel_id, int) else None
        channel_id = extra.get("channel_id")
        return channel_id if isinstance(channel_id, int) else None
    return None


def event_extra_count(event: ModlogEvent) -> int | None:
    extra = event.extra
    if isinstance(extra, dict):
        count = extra.get("count")
        return count if isinstance(count, int) else None
    return None


def same_target(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return anchor.target_id is not None and anchor.target_id == candidate.target_id


def same_channel_if_known(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    anchor_channel_id = event_channel_id(anchor)
    candidate_channel_id = event_channel_id(candidate)
    return (
        anchor_channel_id is None
        or candidate_channel_id is None
        or anchor_channel_id == candidate_channel_id
    )


def target_and_channel(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return same_target(anchor, candidate) and same_channel_if_known(anchor, candidate)


def channel_only(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    anchor_channel_id = event_channel_id(anchor)
    candidate_channel_id = event_channel_id(candidate)
    return (
        anchor_channel_id is not None
        and candidate_channel_id is not None
        and anchor_channel_id == candidate_channel_id
    )


def different_sources(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return anchor.source != candidate.source


def cross_source_target_and_channel(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_sources(anchor, candidate) and target_and_channel(anchor, candidate)


def cross_source_channel_only(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_sources(anchor, candidate) and channel_only(anchor, candidate)


def cross_source_same_target(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_sources(anchor, candidate) and same_target(anchor, candidate)


def bulk_delete_limit(event: ModlogEvent) -> int | None:
    if event.source != "discord_audit_log":
        return 1
    count = event_extra_count(event)
    if count is None:
        return None
    return max(1, count)


def one_related(_event: ModlogEvent) -> int:
    return 1


RELATED_RULES = (
    RelatedRule(
        actions=frozenset({"message_delete", "on_message_delete"}),
        candidate_actions=frozenset({"message_delete", "on_message_delete"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_target_and_channel,
        max_related=one_related,
    ),
    RelatedRule(
        actions=frozenset({"message_bulk_delete", "on_bulk_message_delete"}),
        candidate_actions=frozenset({"message_bulk_delete", "on_bulk_message_delete"}),
        window_seconds=MESSAGE_BULK_WINDOW_SECONDS,
        matches=cross_source_channel_only,
        max_related=bulk_delete_limit,
    ),
    RelatedRule(
        actions=frozenset({"message_update", "on_message_edit"}),
        candidate_actions=frozenset({"message_update", "on_message_edit"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_target_and_channel,
        max_related=one_related,
    ),
    RelatedRule(
        actions=frozenset({"member_role_update", "on_member_role_update"}),
        candidate_actions=frozenset({"member_role_update", "on_member_role_update"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_same_target,
        max_related=one_related,
    ),
    RelatedRule(
        actions=frozenset({"member_update", "on_member_update"}),
        candidate_actions=frozenset({"member_update", "on_member_update"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_same_target,
        max_related=one_related,
    ),
    RelatedRule(
        actions=frozenset({"kick", "on_member_remove"}),
        candidate_actions=frozenset({"kick", "on_member_remove"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_same_target,
        max_related=one_related,
    ),
    RelatedRule(
        actions=frozenset({"ban", "on_member_remove", "on_member_ban"}),
        candidate_actions=frozenset({"ban", "on_member_remove", "on_member_ban"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_same_target,
        max_related=one_related,
    ),
    RelatedRule(
        actions=frozenset({"unban", "on_member_unban"}),
        candidate_actions=frozenset({"unban", "on_member_unban"}),
        window_seconds=DEFAULT_WINDOW_SECONDS,
        matches=cross_source_same_target,
        max_related=one_related,
    ),
)


class RelatedResolver:
    def __init__(self, rules: Iterable[RelatedRule] = RELATED_RULES) -> None:
        self.rules = tuple(rules)

    def rule_for(self, event: ModlogEvent) -> RelatedRule | None:
        return next((rule for rule in self.rules if event.action in rule.actions), None)

    def widened_bounds(self, events: Iterable[ModlogEvent]) -> tuple[int | None, int | None]:
        event_list = list(events)
        if not event_list:
            return None, None
        before_padding = max(
            rule.window_seconds if (rule := self.rule_for(event)) is not None else 0
            for event in event_list
        )
        after_padding = before_padding
        return (
            discord_time_snowflake_offset(min(event.id for event in event_list), -before_padding),
            discord_time_snowflake_offset(max(event.id for event in event_list), after_padding, high=True),
        )

    def group(self, visible_events: Iterable[ModlogEvent], candidate_events: Iterable[ModlogEvent]) -> list[RelatedGroup]:
        candidates_by_id = {
            event.id: event
            for event in candidate_events
        }
        consumed: set[int] = set()
        groups: list[RelatedGroup] = []

        for event in sorted(
            visible_events,
            key=lambda item: (item.source == "discord_gateway", -item.id),
        ):
            if event.id in consumed:
                continue

            rule = self.rule_for(event)
            group = [event]
            consumed.add(event.id)

            if rule is not None:
                candidate_matches = [
                    candidate
                    for candidate in candidates_by_id.values()
                    if candidate.id != event.id
                    and candidate.id not in consumed
                    and candidate.action in rule.candidate_actions
                    and self._within_window(event, candidate, rule.window_seconds)
                    and rule.matches(event, candidate)
                ]
                candidate_matches.sort(key=lambda candidate: (abs(candidate.id - event.id), -candidate.id))
                limit = rule.max_related(event)
                if limit is not None:
                    candidate_matches = candidate_matches[:limit]
                group.extend(candidate_matches)
                consumed.update(candidate.id for candidate in candidate_matches)

            groups.append(RelatedGroup(tuple(sorted(group, key=lambda item: item.id, reverse=True))))

        return sorted(groups, key=lambda group: group.first_id, reverse=True)

    def _within_window(self, anchor: ModlogEvent, candidate: ModlogEvent, seconds: int) -> bool:
        delta = abs((anchor.created_at - candidate.created_at).total_seconds())
        return delta <= seconds
