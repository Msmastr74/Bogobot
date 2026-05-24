from collections import Counter
from typing import Callable, Literal, TypedDict
import asyncio
import json
import os
import random
import time

import discord

from bogobot_core import BotCore
from utils.nl import action


BOGOTREE_N = 12
BOGOTREE_SPLIT_SIZE = 6
BOGOTREE_MIN_STEPS = 73
BOGOTREE_MAX_STEPS = 512
BOGOTREE_STEPS_EXPONENT = 1.5
BOGOTREE_WARMUP_RUNS = 5
BOGOTREE_STORAGE_PATH = "bogotree.json"
BOGOTREE_STATE_KEY = "state"
BOGOTREE_ACCOUNT_KEY = "bogotree"
ARROW = "→"
LEADERBOARD_SECTION_LIMIT = 950
BOGOTREE_SOLVED_SCORE = 1
BOGOTREE_METER_THRESHOLD_CM = 999
BOGOTREE_SEEDLING_MAX_CM = 1000
BOGOTREE_SPROUT_MAX_CM = 10000
BOGOTREE_PSEUDOCODE = f"""```text
Start with {BOGOTREE_N} zeroes, then run {BOGOTREE_WARMUP_RUNS} warm-up runs.

Each /bogotree run simulates {BOGOTREE_MIN_STEPS} to {BOGOTREE_MAX_STEPS} steps.

On each step:
  1. Sort the current values from smallest to largest.
  2. Roll {BOGOTREE_N} random numbers from 1 to {BOGOTREE_N}.
  3. Sort those rolls from largest to smallest.
  4. Add them together slot by slot.

Score each result by how strongly its values cluster together.
The largest matching group matters most, but smaller groups can still help.
The score is scaled non-linearly so high scores get much harder to improve.

If every slot becomes equal, Bogotree is solved.
Otherwise, the shared state moves to the final result from the run.
Best score is only checked against that final result.
```"""

def bogotree_scale(raw_score: float) -> float:
    return raw_score ** 2

class BogotreeState(TypedDict):
    x: list[int]
    current_step: int
    total_steps: int
    best_step: int
    best_score: float
    best_equal_count: int
    best_x: list[int]
    solved: bool


class BogotreeUserStats(TypedDict):
    calls: int
    steps: int
    height: float
    best_score: float
    best_equal_count: int
    best_timestamp: int
    username: str


class BogotreeView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        state: BogotreeState,
        performed_steps: int | None = None,
        previous_height: float | None = None,
        previous_best_score: float | None = None,
        show_info: bool = False,
    ):
        super().__init__(timeout=None)

        x = state["x"]
        tree_lines = render_tree_state(x)
        body_lines = [
            *map(lambda line: f"`{line}`", tree_lines),
            f"Current step: `{state['current_step']:,}`",
            f"Height: `{height_text(x, previous_height)}`",
            f"Current score: `{format_score(bogotree_score(x))}`",
            f"Current in position: `{equal_count(x)}/{len(x)}`",
            f"Best score: `{best_result_text(state, previous_best_score)}`",
            f"Best in position: `{state['best_equal_count']}/{len(x)}`",
            f"Best step: `{state['best_step']:,}`",
        ]
        if performed_steps is not None:
            body_lines.append(f"Run steps: `{performed_steps}`")
        if state["solved"]:
            body_lines.append("Solved. Waiting for a mod reset.")

        best_improved = (
            previous_best_score is not None
            and state["best_score"] > previous_best_score
        )
        accent_colour = discord.Color.gold() if best_improved else discord.Color.blurple()
        if state["solved"] and not best_improved:
            accent_colour = discord.Color.green()

        self.add_item(discord.ui.TextDisplay(f"## {title} {tree_emoji(height(x))}"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(body_lines)),
            accent_colour=accent_colour,
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
            "Height",
            ranked_height(leaderboard),
            lambda uid, stats: f"<@{uid}> `{format_height(stats['height'])}`",
            discord.Color.from_rgb(150, 95, 45),
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
            "Runs",
            ranked_calls(leaderboard),
            lambda uid, stats: f"<@{uid}> `{stats['calls']:,}` runs",
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
            lines.append("-# No data")
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
            f"<@{uid}> `{format_score(stats['best_score'])}`\n"
            f"-# {stats['best_equal_count']}/{BOGOTREE_N} in position · first reached {best_time}"
        )


def ranked_best_score(
    leaderboard: dict[str, BogotreeUserStats],
) -> list[tuple[str, BogotreeUserStats]]:
    return sorted(
        filter(lambda i: i[1]["best_score"] > 0, leaderboard.items()),
        key=lambda item: (
            -item[1]["best_score"],
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
        filter(lambda i: i[1]["steps"] > 0, leaderboard.items()),
        key=lambda item: (
            -item[1]["steps"],
            item[0],
        ),
    )


def ranked_height(
    leaderboard: dict[str, BogotreeUserStats],
) -> list[tuple[str, BogotreeUserStats]]:
    return sorted(
        filter(lambda i: i[1]["height"] > 0, leaderboard.items()),
        key=lambda item: (
            -item[1]["height"],
            item[0],
        ),
    )


def ranked_calls(
    leaderboard: dict[str, BogotreeUserStats],
) -> list[tuple[str, BogotreeUserStats]]:
    return sorted(
        filter(lambda i: i[1]["calls"] > 0, leaderboard.items()),
        key=lambda item: (
            -item[1]["calls"],
            item[0],
        ),
    )


def default_state() -> BogotreeState:
    x = warmup_values()
    return {
        "x": x,
        "current_step": 0,
        "total_steps": 0,
        "best_step": 0,
        "best_score": 0,
        "best_equal_count": 0,
        "best_x": x,
        "solved": False,
    }


def default_user_stats(username: str = "") -> BogotreeUserStats:
    return {
        "calls": 0,
        "steps": 0,
        "height": 0,
        "best_score": 0,
        "best_equal_count": 0,
        "best_timestamp": 0,
        "username": username,
    }


def normalize_user_stats(raw_stats: object, username: str = "") -> BogotreeUserStats:
    if not isinstance(raw_stats, dict):
        return default_user_stats(username)

    try:
        best_equal_count = max(0, min(
            BOGOTREE_N,
            int(raw_stats.get("best_equal_count", 0)),
        ))
        return {
            "calls": max(0, int(raw_stats.get("calls", 0))),
            "steps": max(0, int(raw_stats.get("steps", 0))),
            "height": max(0, float(raw_stats.get("height", 0))),
            "best_score": max(0, float(raw_stats.get("best_score", 0))),
            "best_equal_count": best_equal_count,
            "best_timestamp": max(0, int(raw_stats.get("best_timestamp", 0))),
            "username": str(raw_stats.get("username", username)),
        }
    except (TypeError, ValueError):
        return default_user_stats(username)


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
            best_score = max(
                float(raw_state.get("best_score", 0)),
                bogotree_score(best_x),
            )
        else:
            best_score = max(0, float(raw_state.get("best_score", 0)))
        solved = bool(raw_state.get("solved", False)) or best_equal_count >= BOGOTREE_N
    except (TypeError, ValueError):
        return default_state()

    return {
        "x": x,
        "current_step": current_step,
        "total_steps": total_steps,
        "best_step": best_step,
        "best_score": best_score,
        "best_equal_count": best_equal_count,
        "best_x": best_x,
        "solved": solved,
    }


def normalize_array(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) != BOGOTREE_N:
        raise ValueError("invalid bogotree array")
    return [int(item) for item in value]


def equal_count(values: list[int]) -> int:
    if not values:
        return 0
    return max(Counter(values).values())


def mode_counts(values: list[int]) -> list[int]:
    return sorted(Counter(values).values(), reverse=True)

def mode_values(values: list[int]) -> list[int]:
    return sorted(Counter(values).keys(), reverse=True)


def render_tree_state(values: list[int]) -> list[str]:
    rows: list[list[int]] = []
    for i in range(0, len(values), BOGOTREE_SPLIT_SIZE):
        rows.append(values[i:i + BOGOTREE_SPLIT_SIZE])
    modes = mode_values(values)
    rendered: list[str] = []
    for row in rows:
        tree_line = ""
        state_line = ""
        for i, value in enumerate(row):
            state_segment = f"{value / 100:.2f}"
            if i != len(row) - 1:
                state_segment += " "
            mode_index = modes.index(value) if value in modes else len(modes)
            unicode_char = chr(max(0x2588 - mode_index, 0x2580))
            tree_line += unicode_char * len(state_segment)
            state_line += state_segment
        rendered.extend([tree_line, state_line])
    return rendered


def height(values: list[int]) -> float:
    return sum(values) / len(values) / 100 if values else 0


def tree_emoji(value: float) -> str:
    if value < BOGOTREE_SEEDLING_MAX_CM:
        return "🌱"
    if value < BOGOTREE_SPROUT_MAX_CM:
        return "🪾"
    return "🌳"


def format_height(value: float) -> str:
    if abs(value) > BOGOTREE_METER_THRESHOLD_CM:
        return f"{value / 100:.4f} m"
    return f"{value:.2f} cm"


def format_height_delta(value: float) -> str:
    if abs(value) > BOGOTREE_METER_THRESHOLD_CM:
        return f"{value / 100:+.4f} m"
    precision = 1 if abs(value) >= 1 else 2
    return f"{value:+.{precision}f} cm"


def height_text(values: list[int], previous_height: float | None = None) -> str:
    current_height = height(values)
    if previous_height is None:
        return format_height(current_height)
    return (
        f"{format_height(current_height)} "
        f"{format_height_delta(current_height - previous_height)}"
    )


def bogotree_score(values: list[int]) -> float:
    if sum(values) == 0:
        return 0
    counts = [*mode_counts(values), 0, 0]
    raw_score = counts[0] * 1 + counts[1] * 1/3 + counts[2] * 1/9
    return bogotree_scale(raw_score) / bogotree_scale(BOGOTREE_N)


def score(values: list[int]) -> tuple[float, int, int]:
    return bogotree_score(values), equal_count(values), -spread(values)


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


def non_linear_randint(a: int, b: int, exponent: float) -> int:
    """
    Returns a non-linear integer between a and b (inclusive).
    exponent > 1 biases towards 'a'.
    exponent < 1 biases towards 'b'.
    """
    # 1. Get a random float between 0.0 and 1.0
    r = random.random()
        
    # 2. Apply non-linear transformation
    transformed: float = r ** exponent
    
    # 3. Map to the requested integer range [a, b]
    return a + int(transformed * (b - a + 1))

def simulate_run(values: list[int]) -> tuple[list[int], int, tuple[float, int, int]]:
    current = values
    performed_steps = 0
    for _ in range(non_linear_randint(BOGOTREE_MIN_STEPS, BOGOTREE_MAX_STEPS, BOGOTREE_STEPS_EXPONENT)):
        current = step(current)
        performed_steps += 1
        current_score = score(current)
        if current_score[0] >= BOGOTREE_SOLVED_SCORE:
            return current, performed_steps, current_score
    return current, performed_steps, score(current)


def warmup_values() -> list[int]:
    current = [0 for _ in range(BOGOTREE_N)]
    for _ in range(BOGOTREE_WARMUP_RUNS):
        current, _performed_steps, current_score = simulate_run(current)
        if current_score[1] >= BOGOTREE_N:
            break
    return current


def best_result_text(
    state: BogotreeState,
    previous_best_score: float | None = None,
) -> str:
    current = state["best_score"]
    if previous_best_score is None or previous_best_score == current:
        return format_score(current)
    return f"{format_score(previous_best_score)} {ARROW} {format_score(current)}"

def format_score(score: float) -> str:
    if BOGOTREE_SOLVED_SCORE == 1:
        return f"{score * 100:.3f}%"
    return f"{score:,}"

async def setup(bot: BotCore):
    state_lock = asyncio.Lock()
    storage_path = bot.config.get("bogotree_path", BOGOTREE_STORAGE_PATH)
    sorted_emoji = bot.discord.get_emoji("sorted")
    star_emoji = "⭐"

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
        return {
            uid: normalize_user_stats(raw_stats)
            for uid, raw_stats in await bot.accounts.query(BOGOTREE_ACCOUNT_KEY)
        }

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

    async def update_user_stats(
        interaction: discord.Interaction,
        *,
        calls: int,
        steps: int,
        height_added: float,
        best_score: float,
        best_equal_count: int,
    ) -> None:
        uid = str(interaction.user.id)
        account = bot.accounts[uid]
        stats = normalize_user_stats(
            account.get(BOGOTREE_ACCOUNT_KEY),
            str(interaction.user),
        )
        stats["username"] = str(interaction.user)
        stats["calls"] += calls
        stats["steps"] += steps
        stats["height"] += height_added

        if best_score > stats["best_score"]:
            stats["best_score"] = best_score
            stats["best_equal_count"] = best_equal_count
            stats["best_timestamp"] = int(time.time())

        await account.write(BOGOTREE_ACCOUNT_KEY, stats)

    async def reset_user_scores() -> None:
        for uid, raw_stats in await bot.accounts.query(BOGOTREE_ACCOUNT_KEY):
            stats = normalize_user_stats(raw_stats)
            stats["steps"] = 0
            stats["height"] = 0
            stats["best_score"] = 0
            stats["best_equal_count"] = 0
            stats["best_timestamp"] = 0
            await bot.accounts[uid].write(BOGOTREE_ACCOUNT_KEY, stats)

    @bot.setup.command(
        name="bogotree",
        description="Advance the collaborative bogotree",
        eph=False,
        perm_requirement=0,
    )
    @action(
        "bogotree",
        "Advance the Bogotree puzzle.",
        params={
            "action": (None, Literal["run", "info", "leaderboard"], False),
            "target": (None, discord.User | discord.Member | None),
        },
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
                await reset_user_scores()
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

            performed_steps = 0
            current_step_start = state["current_step"]
            current = state["x"]
            previous_height = height(current)
            best_x = state["best_x"]
            best_step = state["best_step"]
            previous_best_score = state["best_score"]
            best_score = score(best_x) if previous_best_score else (0, 0, 0)

            current, performed_steps, final_score = simulate_run(current)
            state["total_steps"] += performed_steps
            if final_score > best_score:
                best_x = current
                best_step = current_step_start + performed_steps
                best_score = final_score

            state["x"] = current
            state["current_step"] = current_step_start + performed_steps
            state["best_x"] = best_x
            state["best_step"] = best_step
            state["best_score"] = best_score[0]
            state["best_equal_count"] = best_score[1]
            state["solved"] = final_score[1] >= BOGOTREE_N
            best_improved = state["best_score"] > previous_best_score
            height_added = height(current) - previous_height
            await update_user_stats(
                interaction,
                calls=1,
                steps=performed_steps,
                height_added=height_added,
                best_score=final_score[0],
                best_equal_count=final_score[1],
            )
            await save_state(state)

        message = await bot.discord.send(
            view=BogotreeView(
                title="Bogotree Solved" if state["solved"] else "Bogotree",
                state=state,
                performed_steps=performed_steps,
                previous_height=previous_height,
                previous_best_score=previous_best_score,
            ),
            response=True,
        )
        if best_improved and message:
            await message.add_reaction(sorted_emoji)
            await message.add_reaction(star_emoji)
