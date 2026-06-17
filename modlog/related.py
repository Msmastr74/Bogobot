from collections.abc import Iterable
from dataclasses import dataclass

from modlog.actions import ACTIONS, ModlogAction, RelatedRule
from modlog.database import discord_time_snowflake_offset
from modlog.models import ModlogEvent


DEFAULT_WINDOW_SECONDS = 10
MESSAGE_BULK_WINDOW_SECONDS = 30


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


def different_actions(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return anchor.action != candidate.action


def same_actor(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return anchor.actor_id is not None and anchor.actor_id == candidate.actor_id


def cross_source_target_and_channel(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_sources(anchor, candidate) and target_and_channel(anchor, candidate)


def cross_source_channel_only(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_sources(anchor, candidate) and channel_only(anchor, candidate)


def cross_source_same_target(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_sources(anchor, candidate) and same_target(anchor, candidate)


def different_action_same_actor(anchor: ModlogEvent, candidate: ModlogEvent) -> bool:
    return different_actions(anchor, candidate) and same_actor(anchor, candidate)


def audit_log_related_limit(event: ModlogEvent) -> int:
    if event.source != "discord_audit_log":
        return 1
    count = event_extra_count(event)
    if count is None:
        return 1
    return max(1, count)


def related_actions() -> tuple[ModlogAction, ...]:
    return tuple(action for action in ACTIONS.values() if action.related)


class RelatedResolver:
    def __init__(self, actions: Iterable[ModlogAction] | None = None) -> None:
        self.actions = tuple(actions) if actions is not None else related_actions()

    def action_for(self, event: ModlogEvent) -> ModlogAction | None:
        return ACTIONS.get(event.action)

    def relevant_window_seconds(self, event: ModlogEvent) -> int:
        direct = self.action_for(event)
        windows = [
            rule.window_seconds
            for action in self.actions
            for rule in action.related
            if action.name == event.action or event.action in rule.candidate_actions
        ]
        if direct is not None:
            windows.extend(rule.window_seconds for rule in direct.related)
        return max(windows, default=0)

    def widened_bounds(self, events: Iterable[ModlogEvent]) -> tuple[int | None, int | None]:
        event_list = list(events)
        if not event_list:
            return None, None
        before_padding = max(
            self.relevant_window_seconds(event)
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

            group = [event]

            frontier = [event]
            group_ids = {event.id}
            while frontier:
                anchor = frontier.pop()
                candidate_matches = self._candidate_matches(
                    anchor,
                    candidates_by_id.values(),
                    consumed=consumed,
                    group_ids=group_ids,
                )
                for candidate in candidate_matches:
                    group.append(candidate)
                    group_ids.add(candidate.id)
                    frontier.append(candidate)

            consumed.update(group_ids)

            groups.append(RelatedGroup(tuple(sorted(group, key=lambda item: item.id, reverse=True))))

        return sorted(groups, key=lambda group: group.first_id, reverse=True)

    def _within_window(self, anchor: ModlogEvent, candidate: ModlogEvent, seconds: int) -> bool:
        delta = abs((anchor.created_at - candidate.created_at).total_seconds())
        return delta <= seconds

    def _match_from(self, source: ModlogEvent, target: ModlogEvent, action: ModlogAction, rule: RelatedRule) -> bool:
        return (
            source.action == action.name
            and target.action in rule.candidate_actions
            and self._within_window(source, target, rule.window_seconds)
            and rule.matches(source, target)
        )

    def _candidate_matches(
        self,
        anchor: ModlogEvent,
        candidates: Iterable[ModlogEvent],
        *,
        consumed: set[int],
        group_ids: set[int],
    ) -> list[ModlogEvent]:
        candidate_list = list(candidates)
        forward_matches: list[ModlogEvent] = []
        reverse_matches: list[ModlogEvent] = []

        for candidate in candidate_list:
            if candidate.id == anchor.id or candidate.id in consumed or candidate.id in group_ids:
                continue

            for action in self.actions:
                for rule in action.related:
                    if self._match_from(anchor, candidate, action, rule):
                        forward_matches.append(candidate)
                        break
                    if self._match_from(candidate, anchor, action, rule):
                        reverse_matches.append(candidate)
                        break
                else:
                    continue
                break

        forward_matches.sort(key=lambda candidate: (abs(candidate.id - anchor.id), -candidate.id))
        reverse_matches.sort(key=lambda candidate: (abs(candidate.id - anchor.id), -candidate.id))
        if (action := self.action_for(anchor)) is not None and action.related:
            rule = action.related[0]
            limit = rule.max_related(anchor)
            if limit is not None:
                existing_forward_matches = sum(
                    1
                    for candidate in candidate_list
                    if candidate.id != anchor.id
                    and candidate.id in group_ids
                    and self._match_from(anchor, candidate, action, rule)
                )
                limit = max(0, limit - existing_forward_matches)
                forward_matches = forward_matches[:limit]

        return [*forward_matches, *reverse_matches]
