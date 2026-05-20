from typing import Callable, Literal, Mapping, TypedDict
import asyncio
import json
import os
import random
import time

import discord

from bogobot_core import BotCore


CBOGO_N = 10
CBOGO_MIN_SHUFFLES = 1
CBOGO_MAX_SHUFFLES = 100
CBOGO_STORAGE_PATH = "cbogo.json"
CBOGO_STATE_KEY = "state"
CBOGO_ACCOUNT_KEY = "cbogo"
CBOGO_LEADERBOARD_SECTION_LIMIT = 1200
CBOGO_INFO = f"""
### cbogo info

Community Bogosort (cbogo) is the original concept behind `/bogotree`.
Inspired by the tree system from treebot, it's essentially a bogosort where anyone can contribute shuffles to sorting it.
Every `/cbogo` use shuffles the {CBOGO_N} element array {CBOGO_MIN_SHUFFLES}-{CBOGO_MAX_SHUFFLES} times.
Made by <@795401397457125415> and <@1258908783300841654>.
"""

HEIGHT_CHARS = [
    " ", " ̲", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"
]

BOGOGREEN = 0x499D6A
BOGORANGE = 0xDA7656
class CbogoState(TypedDict):
    current_array: list[int]
    shuffles: int
    uses: int
    best_run_shuffle: int
    best_score: int
    best_run_count: int
    best_array: list[int]
    solved: bool
    winner_id: int | None
    winner_name: str | None
    last_user: int | None


class CbogoUserStats(TypedDict):
    uses: int
    shuffles: int
    best_run: int
    best_timestamp: int
    username: str


class CbogoView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        state: CbogoState,
        run_shuffles: int | None = None,
        previous_best_score: int | None = None,
        previous_array: list[int] | None = None,
        show_info = False
    ):
        super().__init__(timeout=None)
        
        run_stats = [f"Shuffled {run_shuffles} times"] if run_shuffles is not None else []

        body_lines = [
            *render_array(state["current_array"], previous_array),
            "-# ■ = correct",
            *run_stats,
            "",
            "**Stats:**",
            f"Shuffles: {state['shuffles']:,}",
            f"Uses: {state['uses']:,}",
            f"Best run: {best_result_text(state, previous_best_score)}",
            f"Achieved first at shuffle {state['best_run_shuffle']:,}",
            f"Times achieved: {state['best_run_count']:,}",
        ]
        if state["solved"]:
            winner = (
                f"<@{state['winner_id']}>"
                if state["winner_id"] is not None
                else state["winner_name"] or "someone"
            )
            body_lines.append(f"Sorted by {winner}.")

        best_improved = (
            previous_best_score is not None
            and state["best_score"] > previous_best_score
        )
        best_equal = (
            previous_best_score is not None
            and in_position(state["current_array"]) == previous_best_score
        )
        accent_colour = discord.Colour.blue()
        if best_improved:
            accent_colour = discord.Colour.gold()
        elif best_equal:
            accent_colour = discord.Colour(BOGOGREEN)
        if state["solved"] and not best_improved:
            accent_colour = discord.Color.light_grey()

        self.add_item(discord.ui.TextDisplay(f"## {title}"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(body_lines)),
            accent_colour=accent_colour,
        ))
        if show_info:
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(CBOGO_INFO),
                accent_colour=discord.Colour(BOGOGREEN)
            ))
        self.add_item(
            discord.ui.TextDisplay(
                f"-# Used at <t:{int(time.time())}:S>"
            )
        )

class CbogoLeaderboard(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        leaderboard: dict[str, CbogoUserStats],
        target: discord.Member | discord.User | None = None,
    ):
        super().__init__(timeout=None)

        self.add_item(discord.ui.TextDisplay("## cbogo leaderboard"))
        self.add_item(self.leaderboard_container(
            "Best Runs",
            ranked_best_runs(leaderboard),
            lambda uid, stats: self.best_run_line(uid, stats),
            discord.Color.gold(),
            target=target,
        ))
        self.add_item(self.leaderboard_container(
            "Shuffles",
            ranked_shuffles(leaderboard),
            lambda uid, stats: f"<@{uid}> — {stats['shuffles']:,} shuffles",
            discord.Colour(BOGOGREEN),
            target=target,
        ))
        self.add_item(self.leaderboard_container(
            "Uses",
            ranked_uses(leaderboard),
            lambda uid, stats: f"<@{uid}> — {stats['uses']:,} uses",
            discord.Colour.blurple(),
            target=target,
        ))

    def leaderboard_container(
        self,
        title: str,
        ranked: list[tuple[str, CbogoUserStats]],
        line_for: Callable[[str, CbogoUserStats], str],
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
        if len(next_text) > CBOGO_LEADERBOARD_SECTION_LIMIT:
            return False
        lines.append(line)
        return True

    def append_target_line(
        self,
        lines: list[str],
        ranked: list[tuple[str, CbogoUserStats]],
        line_for: Callable[[str, CbogoUserStats], str],
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


    def best_run_line(self, uid: str, stats: CbogoUserStats) -> str:
        timestamp = stats["best_timestamp"]
        best_time = f"<t:{timestamp}:f>" if timestamp else "never"
        return (
            f"<@{uid}> — {stats['best_run']}/{CBOGO_N}\n"
            f"-# Achieved first at {best_time}"
        )


def default_array() -> list[int]:
    values = list(range(CBOGO_N))
    while values == sorted(values):
        random.shuffle(values)
    return values


def default_state() -> CbogoState:
    current_array = default_array()
    return {
        "current_array": current_array,
        "shuffles": 0,
        "uses": 0,
        "best_run_shuffle": 0,
        "best_score": 0,
        "best_run_count": 0,
        "best_array": current_array,
        "solved": False,
        "winner_id": None,
        "winner_name": None,
        "last_user": None
    }


def default_user_stats(username: str = "") -> CbogoUserStats:
    return {
        "uses": 0,
        "shuffles": 0,
        "best_run": 0,
        "best_timestamp": 0,
        "username": username,
    }


def normalize_user_stats(raw_stats: object, username: str = "") -> CbogoUserStats:
    if not isinstance(raw_stats, dict):
        return default_user_stats(username)

    try:
        return {
            "uses": max(0, int(raw_stats.get(
                "uses",
                raw_stats.get("runs", 0),
            ))),
            "shuffles": max(0, int(raw_stats.get("shuffles", 0))),
            "best_run": max(0, int(raw_stats.get(
                "best_run",
                raw_stats.get("best_shuffle", 0),
            ))),
            "best_timestamp": max(0, int(raw_stats.get("best_timestamp", 0))),
            "username": str(raw_stats.get("username", username)),
        }
    except (TypeError, ValueError):
        return default_user_stats(username)


def ranked_best_runs(
    leaderboard: dict[str, CbogoUserStats],
) -> list[tuple[str, CbogoUserStats]]:
    return sorted(
        filter(lambda item: item[1]["best_run"] > 0, leaderboard.items()),
        key=lambda item: (
            -item[1]["best_run"],
            item[1]["best_timestamp"] or 2**63 - 1,
            -item[1]["uses"],
            item[0],
        ),
    )


def ranked_shuffles(
    leaderboard: dict[str, CbogoUserStats],
) -> list[tuple[str, CbogoUserStats]]:
    return sorted(
        filter(lambda item: item[1]["shuffles"] > 0, leaderboard.items()),
        key=lambda item: (-item[1]["shuffles"], item[0]),
    )


def ranked_uses(
    leaderboard: dict[str, CbogoUserStats],
) -> list[tuple[str, CbogoUserStats]]:
    return sorted(
        filter(lambda item: item[1]["uses"] > 0, leaderboard.items()),
        key=lambda item: (-item[1]["uses"], item[0]),
    )


def normalize_state(raw_state: object) -> CbogoState:
    if not isinstance(raw_state, dict):
        return default_state()

    try:
        current_array = normalize_array(raw_state.get("current_array"))
        best_array = normalize_array(raw_state.get("best_array", current_array))
        shuffles = max(0, int(raw_state.get(
            "shuffles",
            raw_state.get("current_shuffle", raw_state.get("total_shuffles", 0)),
        )))
        uses = max(0, int(raw_state.get("uses", raw_state.get("runs", 0))))
        best_run_shuffle = max(0, int(raw_state.get(
            "best_run_shuffle",
            raw_state.get("best_shuffle", 0),
        )))
        best_score = max(0, min(CBOGO_N, int(raw_state.get(
            "best_score",
            raw_state.get("best_run", 0),
        ))))
        best_run_count = max(0, int(raw_state.get(
            "best_run_count",
            raw_state.get("best_run_number", 0),
        )))
        if best_run_shuffle > 0:
            best_score = max(best_score, in_position(best_array))
        if best_score > 0:
            best_run_count = max(best_run_count, 1)
        if is_sorted(current_array):
            best_array = current_array
            best_score = CBOGO_N
            best_run_shuffle = best_run_shuffle or shuffles
            best_run_count = max(best_run_count, 1)
        solved = bool(raw_state.get("solved", False)) or best_score >= CBOGO_N
        winner_id = raw_state.get("winner_id")
        if winner_id is not None:
            winner_id = int(winner_id)
        winner_name = raw_state.get("winner_name")
        if winner_name is not None:
            winner_name = str(winner_name)
        last_user = raw_state.get("last_user")
        if last_user is not None:
            last_user = int(last_user)
    except (TypeError, ValueError):
        return default_state()

    return {
        "current_array": current_array,
        "shuffles": shuffles,
        "uses": uses,
        "best_run_shuffle": best_run_shuffle,
        "best_score": best_score,
        "best_run_count": best_run_count,
        "best_array": best_array,
        "solved": solved,
        "winner_id": winner_id,
        "winner_name": winner_name,
        "last_user": last_user
    }


def normalize_array(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) != CBOGO_N:
        raise ValueError("invalid cbogo array")
    values = [int(item) for item in value]
    if sorted(values) != list(range(CBOGO_N)):
        raise ValueError("invalid cbogo values")
    return values


def is_sorted(values: list[int]) -> bool:
    return values == sorted(values)


def in_position(values: list[int]) -> int:
    return sum(1 for index, value in enumerate(values) if index == value)

def value_height_char(value: int) -> str:
    if CBOGO_N <= 1:
        return "█"

    ratio = value / (CBOGO_N - 1)
    index = round(ratio * (len(HEIGHT_CHARS) - 1))
    return HEIGHT_CHARS[index]

def bogo_color(text: str, is_green: bool):
    RED = '\x1b[31m'
    GREEN = '\x1b[32m'
    RESET = '\x1b[0m'
    return f"{GREEN if is_green else RED}{text}{RESET}"

def render_array(
    values: list[int],
    from_values: list[int] | None = None
) -> list[str]:
    width = len(str(CBOGO_N - 1))
    full_width = ((width + 1) * CBOGO_N - 1)

    height_line = " ".join(
        bogo_color(value_height_char(value) * width, value == index)
        for index, value in enumerate(values)
    ) + f" {in_position(values)}/{CBOGO_N}"
    
    bar_line = " ".join(
        ("■" if value == index else "□") * width
        for index, value in enumerate(values)
    ) # For mobile, which does not support ANSI colours
    current_line = " ".join(
        str(value).rjust(width)
        for value in values
    )
    current_line = f"\x1b[37m{current_line}\x1b[0m"

    return [
        "```ansi",
        height_line,
        bar_line,
        current_line,
        *([
            f"\x1b[37m{'from:'.center(full_width)}\x1b[0m",
            *render_previous_array(from_values)
        ] if from_values is not None else []),
        "```"
    ]

def render_previous_array(
    values: list[int]
) -> list[str]:
    width = len(str(CBOGO_N - 1))

    height_line = " ".join(
        value_height_char(value) * width
        for value in values
    ) + f" {in_position(values)}/{CBOGO_N}"
    
    bar_line = " ".join(
        ("■" if value == index else " ") * width
        for index, value in enumerate(values)
    )
    current_line = " ".join(
        str(value).rjust(width)
        for value in values
    )
    current_line = f"\x1b[37m{current_line}\x1b[0m"

    return [
        height_line,
        bar_line,
        current_line,
    ]

def best_result_text(state: CbogoState, previous_best_score: int | None = None) -> str:
    current = state["best_score"]
    if previous_best_score is None or previous_best_score == current:
        return f"{current}/{CBOGO_N}"
    return f"{previous_best_score}/{CBOGO_N} → {current}/{CBOGO_N}"

def get_shuffle_count(min_count: int, max_count: int) -> int:
    if min_count >= max_count:
        return min_count
    
    t = random.betavariate(2.5, 2.5)
    span = max_count - min_count + 1
    val = min_count + int(t * span)
    return min(val, max_count)

def run_shuffles(values: list[int]) -> tuple[list[int], int, int]:
    current = values[:]
    shuffles = get_shuffle_count(CBOGO_MIN_SHUFFLES, CBOGO_MAX_SHUFFLES)
    best_score = -1
    best_array = current[:]

    for performed in range(1, shuffles + 1):
        random.shuffle(current)
        current_score = in_position(current)

        if current_score > best_score:
            best_score = current_score
            best_array = current[:]

        if is_sorted(current):
            return current, performed, CBOGO_N

    if best_score == -1:
      best_score = in_position(best_array)
    return best_array, shuffles, best_score

async def setup(bot: BotCore):
    state_lock = asyncio.Lock()
    storage_path = bot.config.get("cbogo_path", CBOGO_STORAGE_PATH)
    sorted_emoji = bot.discord.get_emoji("sorted")

    def load_storage_sync() -> dict[str, object]:
        if not os.path.exists(storage_path):
            return {}

        try:
            with open(storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        return data if isinstance(data, dict) else {}

    def save_storage_sync(storage: Mapping[str, object]) -> None:
        directory = os.path.dirname(storage_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        tmp_path = f"{storage_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(storage, f, indent=4)
        os.replace(tmp_path, storage_path)

    async def load_storage() -> dict[str, object]:
        return await asyncio.to_thread(load_storage_sync)

    async def save_storage(storage: Mapping[str, object]) -> None:
        await asyncio.to_thread(save_storage_sync, storage)

    async def get_state() -> CbogoState:
        storage = await load_storage()
        state = normalize_state(storage.get(CBOGO_STATE_KEY, storage))
        if state != storage.get(CBOGO_STATE_KEY):
            storage[CBOGO_STATE_KEY] = state
            await save_storage(storage)
        return state

    async def save_state(state: CbogoState) -> None:
        storage = await load_storage()
        storage[CBOGO_STATE_KEY] = state
        await save_storage(storage)

    async def get_leaderboard() -> dict[str, CbogoUserStats]:
        return {
            uid: normalize_user_stats(raw_stats)
            for uid, raw_stats in await bot.accounts.query(CBOGO_ACCOUNT_KEY)
        }

    async def cbogo_leaderboard(
        interaction: discord.Interaction,
        target: discord.Member | discord.User | None = None,
    ):
        leaderboard = await get_leaderboard()
        await bot.discord.send(
            view=CbogoLeaderboard(
                leaderboard=leaderboard,
                target=target,
            ),
            response=True,
            safety_filter=True,
        )

    async def reset_user_scores() -> None:
        for uid, raw_stats in await bot.accounts.query(CBOGO_ACCOUNT_KEY):
            stats = normalize_user_stats(raw_stats)
            stats["shuffles"] = 0
            stats["best_run"] = 0
            stats["best_timestamp"] = 0
            await bot.accounts[uid].write(CBOGO_ACCOUNT_KEY, stats)

    async def update_user_stats(
        interaction: discord.Interaction,
        *,
        uses: int,
        shuffles: int,
        best_run: int,
    ) -> None:
        uid = str(interaction.user.id)
        account = bot.accounts[uid]
        stats = normalize_user_stats(
            account.get(CBOGO_ACCOUNT_KEY),
            str(interaction.user),
        )
        stats["username"] = str(interaction.user)
        stats["uses"] += uses
        stats["shuffles"] += shuffles
        if best_run > stats["best_run"]:
            stats["best_run"] = best_run
            stats["best_timestamp"] = int(time.time())
        await account.write(CBOGO_ACCOUNT_KEY, stats)

    @bot.setup.command(
        name="cbogo",
        description="Run cbogo",
        defer=False,
        perm_requirement=0,
    )
    async def cbogo(
        interaction: discord.Interaction,
        action: Literal["run", "info", "leaderboard", "reset", "reset_last_user"] = "run",
        target: discord.Member | discord.User | None = None,
    ):
        if action == "reset":
            if not bot.is_authorized(interaction.user.id, 2):
                await bot.discord.send(
                    "Only mods can reset cbogo.",
                    response=True,
                    ephemeral=True,
                )
                return

            async with state_lock:
                state = default_state()
                await save_state(state)
                await reset_user_scores()
            await bot.discord.send(
                view=CbogoView(title="cbogo reset", state=state),
                response=True,
                safety_filter=True
            )
            return
        if action == "reset_last_user":
            if not bot.is_authorized(interaction.user.id, 1):
                await bot.discord.send(
                    "Unauthorized.",
                    response=True,
                    ephemeral=True
                )
                return
            async with state_lock:
                state = await get_state()
                old_last_user = state["last_user"]
                state["last_user"] = None
                await save_state(state)
            if old_last_user is None:
                await bot.discord.send(
                    "No last user was set.",
                    response=True,
                    ephemeral=True
                )
                return
            await bot.discord.send(
                f"Reset last user from <@{old_last_user}> to None.",
                response=True,
                ephemeral=True,
                safety_filter=True
            )
            return

        if action == "leaderboard":
            await cbogo_leaderboard(interaction, target=target)
            return

        if action == "info":
            state = await get_state()
            await bot.discord.send(
                view=CbogoView(
                    title="cbogo info",
                    state=state,
                    show_info=True
                ),
                response=True,
                safety_filter=True
            )
            return

        async with state_lock:
            state = await get_state()
            if state["solved"]:
                await bot.discord.send(
                    view=CbogoView(title="cbogo sorted", state=state),
                    response=True,
                    safety_filter=True
                )
                return
            if state["last_user"] == interaction.user.id:
                await bot.discord.send(
                    "You cannot use cbogo twice in a row!",
                    response=True,
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
            state["last_user"] = interaction.user.id

            shuffle_start = state["shuffles"]
            previous_best_score = state["best_score"]
            previous_array = state["current_array"]

            current, performed, run_best_score = run_shuffles(state["current_array"])

            state["current_array"] = current
            state["shuffles"] = shuffle_start + performed
            state["uses"] += 1
            if run_best_score > state["best_score"]:
                state["best_score"] = run_best_score
                state["best_array"] = current
                state["best_run_shuffle"] = state["shuffles"]
                state["best_run_count"] = 1
            elif run_best_score > 0 and run_best_score == state["best_score"]:
                state["best_run_count"] += 1
            state["solved"] = run_best_score >= CBOGO_N
            if state["solved"]:
                state["winner_id"] = interaction.user.id
                state["winner_name"] = str(interaction.user)
            await update_user_stats(
                interaction,
                uses=1,
                shuffles=performed,
                best_run=run_best_score,
            )
            await save_state(state)

        message = await bot.discord.send(
            view=CbogoView(
                title=f"cbogo {sorted_emoji}" if state["solved"] else "cbogo",
                state=state,
                run_shuffles=performed,
                previous_best_score=previous_best_score,
                previous_array=previous_array,
            ),
            response=True,
            safety_filter=True
        )
        if state["solved"] and message:
            await message.add_reaction(sorted_emoji)
