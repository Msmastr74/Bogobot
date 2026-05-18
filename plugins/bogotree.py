from collections import Counter
from typing import Callable, Literal, TypedDict
import asyncio
import json
import os
import random
import time

import discord

from bogobot_core import BotCore


BOGOTREE_N = 10
BOGOTREE_MIN_STEPS = 30
BOGOTREE_MAX_STEPS = 100
BOGOTREE_STORAGE_PATH = "bogotree.json"
BOGOTREE_STATE_KEY = "state"
BOGOTREE_LEADERBOARD_KEY = "leaderboard"
ARROW = "\u2192"
LEADERBOARD_SECTION_LIMIT = 1200
BOGOTREE_PSEUDOCODE = """```text
x = Array(n).fill(0)

each /bogotree run:
  candidate = x
  batch_best = x
  repeat random(MIN_STEPS..MAX_STEPS) times:
    r = Array(n).fill_each(randint(1..n))
    candidate = sorted(candidate) + sorted_desc(r)
    remember the last candidate with the best equal-slot count
    stop forever if all slots are equal
  x = batch_best
```"""


class BogotreeState(TypedDict):
    x: list[int]
    current_step: int
    total_steps: int
    best_step: int
    best_equal_count: int
    best_x: list[int]
    solved: bool


class BogotreeUserStats(TypedDict):
    calls: int
    steps: int
    best_equal_count: int
    best_timestamp: int
    username: str


class BogotreeView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        state: BogotreeState,
        batch_steps: int | None = None,
        selected_steps: int | None = None,
        previous_best_equal_count: int | None = None,
        show_info: bool = False,
    ):
        super().__init__(timeout=None)

        x = state["x"]
        body_lines = [
            f"State: `{format_array(x)}`",
            f"Current step: `{state['current_step']:,}`",
            f"Current in position: `{equal_count(x)}/{len(x)}`",
            f"Best result: `{best_result_text(state, previous_best_equal_count)}`",
            f"Best step: `{state['best_step']:,}`",
            f"Total simulated: `{state['total_steps']:,}`",
        ]
        if batch_steps is not None:
            body_lines.append(f"Batch simulated: `{batch_steps}` steps")
        if selected_steps is not None:
            body_lines.append(f"Batch selected: `{selected_steps}` steps")
        if state["solved"]:
            body_lines.append("Solved. Waiting for a mod reset.")

        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(body_lines)),
            accent_colour=discord.Color.green() if state["solved"] else discord.Color.blurple(),
        ))
        if show_info:
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(
                    "**Pseudocode**\n"
                    f"{BOGOTREE_PSEUDOCODE}"
                ),
                accent_colour=discord.Color.dark_teal(),
            ))


class BogotreeLeaderboard(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        leaderboard: dict[str, BogotreeUserStats],
        target: discord.Member | discord.User | None = None,
    ):
        super().__init__(timeout=None)

        self.add_item(discord.ui.TextDisplay("## Bogotree Leaderboard"))
        self.add_item(self.leaderboard_container(
            "Best Score",
            ranked_best_score(leaderboard),
            lambda uid, stats: self.best_score_line(uid, stats),
            discord.Color.gold(),
            target=target,
        ))
        self.add_item(self.leaderboard_container(
            "Steps",
            ranked_steps(leaderboard),
            lambda uid, stats: f"<@{uid}> `{stats['steps']:,}` steps",
            discord.Color.blurple(),
            target=target,
        ))
        self.add_item(self.leaderboard_container(
            "Uses",
            ranked_calls(leaderboard),
            lambda uid, stats: f"<@{uid}> `{stats['calls']:,}` uses",
            discord.Color.dark_teal(),
            target=target,
        ))

    def leaderboard_container(
        self,
        title: str,
        ranked: list[tuple[str, BogotreeUserStats]],
        line_for: Callable[[str, BogotreeUserStats], str],
        colour: discord.Color,
        *,
        target: discord.Member | discord.User | None = None,
    ) -> discord.ui.Container:
        lines = [f"### {title}"]
        visible_uids: set[str] = set()
        if not ranked:
            lines.append("No bogotree runs yet.")
        else:
            for index, (uid, stats) in enumerate(ranked, start=1):
                line = f"`#{index}` {line_for(uid, stats)}"
                if not self.try_append_line(lines, line):
                    self.try_append_line(lines, "...")
                    break
                visible_uids.add(uid)

        if target is not None and str(target.id) not in visible_uids:
            self.append_target_line(lines, ranked, line_for, target)

        return discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_colour=colour,
        )

    def try_append_line(self, lines: list[str], line: str) -> bool:
        next_text = "\n".join([*lines, line])
        if len(next_text) > LEADERBOARD_SECTION_LIMIT:
            return False
        lines.append(line)
        return True

    def append_target_line(
        self,
        lines: list[str],
        ranked: list[tuple[str, BogotreeUserStats]],
        line_for: Callable[[str, BogotreeUserStats], str],
        target: discord.Member | discord.User,
    ) -> None:
        uid = str(target.id)
        rank = None
        stats = default_user_stats(str(target))
        for index, (ranked_uid, ranked_stats) in enumerate(ranked, start=1):
            if ranked_uid == uid:
                rank = index
                stats = ranked_stats
                break

        prefix = f"`#{rank}`" if rank is not None else "`--`"
        line = f"{prefix} {line_for(uid, stats)}"

        while lines and not self.try_append_line(lines, line):
            removed = lines.pop()
            if removed == "...":
                continue
            if len(lines) == 1:
                self.try_append_line(lines, "...")
                break
            if lines[-1] != "...":
                self.try_append_line(lines, "...")

    def best_score_line(self, uid: str, stats: BogotreeUserStats) -> str:
        timestamp = stats["best_timestamp"]
        best_time = f"<t:{timestamp}:T>" if timestamp else "never"
        return (
            f"<@{uid}> `{stats['best_equal_count']}/{BOGOTREE_N}`\n"
            f"-# first reached {best_time}"
        )


def ranked_best_score(
    leaderboard: dict[str, BogotreeUserStats],
) -> list[tuple[str, BogotreeUserStats]]:
    return sorted(
        leaderboard.items(),
        key=lambda item: (
            -item[1]["best_equal_count"],
            item[1]["best_timestamp"] or 2**63 - 1,
            -item[1]["steps"],
            item[0],
        ),
    )


def ranked_steps(
    leaderboard: dict[str, BogotreeUserStats],
) -> list[tuple[str, BogotreeUserStats]]:
    return sorted(
        leaderboard.items(),
        key=lambda item: (
            -item[1]["steps"],
            item[0],
        ),
    )


def ranked_calls(
    leaderboard: dict[str, BogotreeUserStats],
) -> list[tuple[str, BogotreeUserStats]]:
    return sorted(
        leaderboard.items(),
        key=lambda item: (
            -item[1]["calls"],
            item[0],
        ),
    )


def default_state() -> BogotreeState:
    return {
        "x": [0 for _ in range(BOGOTREE_N)],
        "current_step": 0,
        "total_steps": 0,
        "best_step": 0,
        "best_equal_count": 0,
        "best_x": [0 for _ in range(BOGOTREE_N)],
        "solved": False,
    }


def default_user_stats(username: str = "") -> BogotreeUserStats:
    return {
        "calls": 0,
        "steps": 0,
        "best_equal_count": 0,
        "best_timestamp": 0,
        "username": username,
    }


def normalize_state(raw_state: object) -> BogotreeState:
    if not isinstance(raw_state, dict):
        return default_state()

    try:
        x = normalize_array(raw_state.get("x"))
        best_x = normalize_array(raw_state.get("best_x", x))
        total_steps = max(0, int(raw_state.get("total_steps", 0)))
        current_step = max(0, int(raw_state.get("current_step", total_steps)))
        best_step = max(0, int(raw_state.get("best_step", total_steps)))
        best_equal_count = max(0, int(raw_state.get("best_equal_count", 0)))
        if best_step > 0:
            best_equal_count = max(best_equal_count, equal_count(best_x))
        solved = bool(raw_state.get("solved", False)) or best_equal_count >= BOGOTREE_N
    except (TypeError, ValueError):
        return default_state()

    return {
        "x": x,
        "current_step": current_step,
        "total_steps": total_steps,
        "best_step": best_step,
        "best_equal_count": best_equal_count,
        "best_x": best_x,
        "solved": solved,
    }


def normalize_leaderboard(raw_leaderboard: object) -> dict[str, BogotreeUserStats]:
    if not isinstance(raw_leaderboard, dict):
        return {}

    leaderboard: dict[str, BogotreeUserStats] = {}
    for raw_uid, raw_stats in raw_leaderboard.items():
        uid = str(raw_uid)
        if not isinstance(raw_stats, dict):
            continue

        try:
            leaderboard[uid] = {
                "calls": max(0, int(raw_stats.get("calls", 0))),
                "steps": max(0, int(raw_stats.get("steps", 0))),
                "best_equal_count": max(0, min(
                    BOGOTREE_N,
                    int(raw_stats.get("best_equal_count", 0)),
                )),
                "best_timestamp": max(0, int(raw_stats.get("best_timestamp", 0))),
                "username": str(raw_stats.get("username", "")),
            }
        except (TypeError, ValueError):
            continue

    return leaderboard


def normalize_array(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) != BOGOTREE_N:
        raise ValueError("invalid bogotree array")
    return [int(item) for item in value]


def equal_count(values: list[int]) -> int:
    if not values:
        return 0
    return max(Counter(values).values())


def score(values: list[int]) -> tuple[int, int]:
    return equal_count(values), -spread(values)


def spread(values: list[int]) -> int:
    return max(values) - min(values) if values else 0


def step(values: list[int]) -> list[int]:
    additions = sorted(
        (random.randint(1, BOGOTREE_N) for _ in range(BOGOTREE_N)),
        reverse=True,
    )
    return [
        current + addition
        for current, addition in zip(sorted(values), additions)
    ]


def format_array(values: list[int]) -> str:
    return ", ".join(str(value) for value in values)


def best_result_text(
    state: BogotreeState,
    previous_best_equal_count: int | None = None,
) -> str:
    current = state["best_equal_count"]
    if previous_best_equal_count is None or previous_best_equal_count == current:
        return f"{current}/{len(state['x'])}"
    return f"{previous_best_equal_count}/{len(state['x'])} {ARROW} {current}/{len(state['x'])}"


async def setup(bot: BotCore):
    state_lock = asyncio.Lock()
    storage_path = bot.config.get("bogotree_path", BOGOTREE_STORAGE_PATH)

    def load_storage_sync() -> dict[str, object]:
        if not os.path.exists(storage_path):
            return {}

        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        return data if isinstance(data, dict) else {}

    def save_storage_sync(storage: dict[str, object]) -> None:
        directory = os.path.dirname(storage_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{storage_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(storage, f, indent=4)
        os.replace(tmp_path, storage_path)

    async def load_storage() -> dict[str, object]:
        return await asyncio.to_thread(load_storage_sync)

    async def save_storage(storage: dict[str, object]) -> None:
        await asyncio.to_thread(save_storage_sync, storage)

    async def get_state() -> BogotreeState:
        storage = await load_storage()
        state = normalize_state(storage.get(BOGOTREE_STATE_KEY))
        if state != storage.get(BOGOTREE_STATE_KEY):
            storage[BOGOTREE_STATE_KEY] = state
            await save_storage(storage)
        return state

    async def save_state(state: BogotreeState) -> None:
        storage = await load_storage()
        storage[BOGOTREE_STATE_KEY] = state
        await save_storage(storage)

    async def get_leaderboard() -> dict[str, BogotreeUserStats]:
        storage = await load_storage()
        leaderboard = normalize_leaderboard(storage.get(BOGOTREE_LEADERBOARD_KEY))
        if leaderboard != storage.get(BOGOTREE_LEADERBOARD_KEY):
            storage[BOGOTREE_LEADERBOARD_KEY] = leaderboard
            await save_storage(storage)
        return leaderboard

    async def save_state_and_leaderboard(
        state: BogotreeState,
        leaderboard: dict[str, BogotreeUserStats],
    ) -> None:
        storage = await load_storage()
        storage[BOGOTREE_STATE_KEY] = state
        storage[BOGOTREE_LEADERBOARD_KEY] = leaderboard
        await save_storage(storage)

    async def bogotree_leaderboard(
        interaction: discord.Interaction,
        target: discord.Member | discord.User | None = None,
    ):
        leaderboard = await get_leaderboard()
        await bot.discord.send(
            view=BogotreeLeaderboard(
                leaderboard=leaderboard,
                target=target,
            ),
            response=True,
            safety_filter=True
        )

    def update_user_stats(
        leaderboard: dict[str, BogotreeUserStats],
        interaction: discord.Interaction,
        *,
        calls: int,
        steps: int,
        best_equal_count: int,
    ) -> None:
        uid = str(interaction.user.id)
        stats = leaderboard.get(uid, default_user_stats(str(interaction.user)))
        stats["username"] = str(interaction.user)
        stats["calls"] += calls
        stats["steps"] += steps

        if best_equal_count > stats["best_equal_count"]:
            stats["best_equal_count"] = best_equal_count
            stats["best_timestamp"] = int(time.time())

        leaderboard[uid] = stats

    @bot.setup.command(
        name="bogotree",
        description="Advance the collaborative bogotree",
        eph=False,
        perm_requirement=0,
    )
    async def bogotree(
        interaction: discord.Interaction,
        action: Literal["run", "info", "leaderboard", "reset"] = "run",
        target: discord.Member | discord.User | None = None,
    ):
        if action == "reset":
            if not bot.is_authorized(interaction.user.id, 2):
                await bot.discord.send(
                    "Only mods can reset bogotree.",
                    response=True,
                    ephemeral=True,
                )
                return

            async with state_lock:
                state = default_state()
                await save_state(state)
            await bot.discord.send(
                view=BogotreeView(title="Bogotree Reset", state=state),
                response=True,
            )
            return

        if action == "leaderboard":
            await bogotree_leaderboard(interaction, target=target)
            return

        if action == "info":
            state = await get_state()
            await bot.discord.send(
                view=BogotreeView(
                    title="Bogotree Info",
                    state=state,
                    show_info=True,
                ),
                response=True,
            )
            return

        async with state_lock:
            state = await get_state()
            if state["solved"]:
                await bot.discord.send(
                    view=BogotreeView(title="Bogotree", state=state),
                    response=True,
                )
                return

            planned_steps = random.randint(BOGOTREE_MIN_STEPS, BOGOTREE_MAX_STEPS)
            leaderboard = await get_leaderboard()
            performed_steps = 0
            selected_steps = 0
            current_step_start = state["current_step"]
            current = state["x"]
            best_x = state["best_x"]
            best_step = state["best_step"]
            previous_best_equal_count = state["best_equal_count"]
            best_score = state["best_equal_count"], -spread(best_x)
            batch_best_x = current
            batch_best_score: tuple[int, int] | None = None
            batch_best_offset = 0

            for _ in range(planned_steps):
                current = step(current)
                performed_steps += 1
                state["total_steps"] += 1
                current_score = score(current)
                if batch_best_score is None or current_score >= batch_best_score:
                    batch_best_x = current
                    batch_best_score = current_score
                    batch_best_offset = performed_steps
                if current_score > best_score:
                    best_x = current
                    best_step = current_step_start + performed_steps
                    best_score = current_score
                    if best_score[0] >= BOGOTREE_N:
                        break

            selected_steps = batch_best_offset
            state["x"] = batch_best_x
            state["current_step"] = current_step_start + selected_steps
            state["best_x"] = best_x
            state["best_step"] = best_step
            state["best_equal_count"] = best_score[0]
            state["solved"] = best_score[0] >= BOGOTREE_N
            update_user_stats(
                leaderboard,
                interaction,
                calls=1,
                steps=performed_steps,
                best_equal_count=batch_best_score[0] if batch_best_score is not None else 0,
            )
            await save_state_and_leaderboard(state, leaderboard)

        await bot.discord.send(
            view=BogotreeView(
                title="Bogotree Solved" if state["solved"] else "Bogotree",
                state=state,
                batch_steps=performed_steps,
                selected_steps=selected_steps,
                previous_best_equal_count=previous_best_equal_count,
            ),
            response=True,
        )
