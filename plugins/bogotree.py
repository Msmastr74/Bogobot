from collections import Counter
from typing import Literal, TypedDict
import asyncio
import random

import discord

from bogobot_core import BotCore


BOGOTREE_N = 10
BOGOTREE_MIN_STEPS = 30
BOGOTREE_MAX_STEPS = 100
BOGOTREE_CONFIG_KEY = "bogotree"
ARROW = "\u2192"
BOGOTREE_PSEUDOCODE = """```text
x = Array(n).fill(0)

each /bogotree run:
  repeat random(MIN_STEPS..MAX_STEPS) times:
    r = Array(n).fill_each(randint(1..n))
    x = sorted(x) + sorted_desc(r)
    remember x if it has the best equal-slot count
    stop forever if all slots are equal
```"""


class BogotreeState(TypedDict):
    x: list[int]
    total_steps: int
    best_step: int
    best_equal_count: int
    best_x: list[int]
    solved: bool


class BogotreeView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        state: BogotreeState,
        batch_steps: int | None = None,
        previous_best_equal_count: int | None = None,
        show_info: bool = False,
    ):
        super().__init__(timeout=None)

        x = state["x"]
        body_lines = [
            f"State: `{format_array(x)}`",
            f"Current step: `{state['total_steps']:,}`",
            f"Current in position: `{equal_count(x)}/{len(x)}`",
            f"Best result: `{best_result_text(state, previous_best_equal_count)}`",
            f"Best step: `{state['best_step']:,}`",
            f"Total simulated: `{state['total_steps']:,}`",
        ]
        if batch_steps is not None:
            body_lines.append(f"Batch: `{batch_steps}` steps")
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


def default_state() -> BogotreeState:
    return {
        "x": [0 for _ in range(BOGOTREE_N)],
        "total_steps": 0,
        "best_step": 0,
        "best_equal_count": 0,
        "best_x": [0 for _ in range(BOGOTREE_N)],
        "solved": False,
    }


def normalize_state(raw_state: object) -> BogotreeState:
    if not isinstance(raw_state, dict):
        return default_state()

    try:
        x = normalize_array(raw_state.get("x"))
        best_x = normalize_array(raw_state.get("best_x", x))
        total_steps = max(0, int(raw_state.get("total_steps", 0)))
        best_step = max(0, int(raw_state.get("best_step", total_steps)))
        best_equal_count = max(0, int(raw_state.get("best_equal_count", 0)))
        if best_step > 0:
            best_equal_count = max(best_equal_count, equal_count(best_x))
        solved = bool(raw_state.get("solved", False)) or best_equal_count >= BOGOTREE_N
    except (TypeError, ValueError):
        return default_state()

    return {
        "x": x,
        "total_steps": total_steps,
        "best_step": best_step,
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
        return str(current)
    return f"{previous_best_equal_count} {ARROW} {current}"


async def setup(bot: BotCore):
    state_lock = asyncio.Lock()

    async def get_state() -> BogotreeState:
        state = normalize_state(bot.config.get(BOGOTREE_CONFIG_KEY))
        if state != bot.config.get(BOGOTREE_CONFIG_KEY):
            bot.config[BOGOTREE_CONFIG_KEY] = state
            await bot.save_config()
        return state

    async def save_state(state: BogotreeState) -> None:
        bot.config[BOGOTREE_CONFIG_KEY] = state
        await bot.save_config()

    @bot.setup.command(
        name="bogotree",
        description="Advance the collaborative bogotree",
        eph=False,
        perm_requirement=0,
    )
    async def bogotree(
        interaction: discord.Interaction,
        action: Literal["run", "info", "reset"] = "run",
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
            performed_steps = 0
            current = state["x"]
            best_x = state["best_x"]
            best_step = state["best_step"]
            previous_best_equal_count = state["best_equal_count"]
            best_score = state["best_equal_count"], -spread(best_x)

            for _ in range(planned_steps):
                current = step(current)
                performed_steps += 1
                state["total_steps"] += 1
                current_score = score(current)
                if current_score > best_score:
                    best_x = current
                    best_step = state["total_steps"]
                    best_score = current_score
                    if best_score[0] >= BOGOTREE_N:
                        break

            state["x"] = current
            state["best_x"] = best_x
            state["best_step"] = best_step
            state["best_equal_count"] = best_score[0]
            state["solved"] = best_score[0] >= BOGOTREE_N
            await save_state(state)

        await bot.discord.send(
            view=BogotreeView(
                title="Bogotree Solved" if state["solved"] else "Bogotree",
                state=state,
                batch_steps=performed_steps,
                previous_best_equal_count=previous_best_equal_count,
            ),
            response=True,
        )
