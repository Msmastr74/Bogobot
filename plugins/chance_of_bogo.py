import math
from decimal import Decimal
from fractions import Fraction

import discord

from ai import AIParam, action
from bogobot_core import BotCore
from plugins.stats import parse_number


SORT_SIZE = 25


def subfactorial(n: int) -> int:
    """Return the number of derangements of n items."""
    if n < 0:
        raise ValueError("n must be nonnegative")
    if n == 0:
        return 1
    if n == 1:
        return 0

    previous, current = 1, 0
    for k in range(2, n + 1):
        previous, current = current, (k - 1) * (current + previous)
    return current


def exact_matches_probability(matches: int, total: int = SORT_SIZE) -> Fraction:
    if not 0 <= matches <= total:
        raise ValueError(f"n must be between 0 and {total}")

    unmatched = total - matches
    return Fraction(
        subfactorial(unmatched),
        math.factorial(matches) * math.factorial(unmatched),
    )


def at_least_matches_probability(matches: int, total: int = SORT_SIZE) -> Fraction:
    if not 0 <= matches <= total:
        raise ValueError(f"n must be between 0 and {total}")
    return sum(
        (exact_matches_probability(value, total) for value in range(matches, total + 1)),
        start=Fraction(),
    )


def repeated_chance(probability: Fraction, shuffles: int) -> float:
    """Return the percentage chance of at least one success."""
    if shuffles < 0:
        raise ValueError("shuffles must be nonnegative")
    if probability == 0 or shuffles == 0:
        return 0.0
    if probability == 1:
        return 100.0

    probability_float = float(probability)
    return -math.expm1(shuffles * math.log1p(-probability_float)) * 100.0


def _format_percent(value: float) -> str:
    if value == 0:
        return "0%"
    if value >= 99.999999:
        return "100%"
    if value < 0.000001:
        return f"{value:.3e}%"
    return f"{value:.6f}".rstrip("0").rstrip(".") + "%"


def _format_duration(seconds: Decimal) -> str:
    if seconds < 1:
        return "less than 1 second"

    seconds_int = int(seconds)
    minute, hour, day, year = 60, 3_600, 86_400, 31_557_600
    if seconds_int >= year:
        return f"{Decimal(seconds_int) / year:,.2f} years"
    if seconds_int >= day:
        return f"{Decimal(seconds_int) / day:,.2f} days"
    if seconds_int >= hour:
        return f"{Decimal(seconds_int) / hour:,.2f} hours"
    if seconds_int >= minute:
        return f"{Decimal(seconds_int) / minute:,.2f} minutes"
    return f"{seconds_int:,} seconds"


class ChanceOfBogoView(discord.ui.LayoutView):
    def __init__(self, *, n: int, shuffles: int, shuffles_sec: Decimal) -> None:
        super().__init__(timeout=None)

        probability = at_least_matches_probability(n)
        happened_chance = repeated_chance(probability, shuffles)
        score_text = f"`{SORT_SIZE}`" if n >= SORT_SIZE-1 else f"`{n}` or more"
        expected_shuffles = (
            probability.denominator + probability.numerator - 1
        ) // probability.numerator
        expected_seconds = Decimal(expected_shuffles) / shuffles_sec

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Chance of Bogo"),
            discord.ui.TextDisplay(
                f"Chance that **{score_text}** correct positions occurred over "
                f"`{shuffles:,}` shuffles:\n"
                f"### {_format_percent(happened_chance)}"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"Expected shuffles to get **{score_text}**: `{expected_shuffles:,}`\n"
                f"Expected time at `{shuffles_sec:,.0f}` shuffles/second: "
                f"`{_format_duration(expected_seconds)}`"
            ),
        ))


async def setup(bot: BotCore) -> None:
    @bot.setup.command(
        name="chance_of_bogo",
        description="Calculate the chance of getting a given Bogo score.",
        defer=False,
    )
    @action(
        "chance_of_bogo",
        "Calculate the chance that a score or better occurred over all Bogostream shuffles.",
        params={"n": AIParam("The minimum score from 0 to 25.", type=int)},
    )
    async def chance_of_bogo(interaction: discord.Interaction, n: int) -> None:
        shuffles = parse_number(bot.stats.get("shuffles"))
        shuffles_sec = parse_number(bot.stats.get("shuffles_sec"))
        if shuffles is None or shuffles_sec is None or shuffles_sec <= 0:
            await bot.discord.send(
                "Bogostream shuffle statistics are not available yet.",
                response=True,
                ephemeral=True,
            )
            return

        try:
            view = ChanceOfBogoView(
                n=n,
                shuffles=int(shuffles),
                shuffles_sec=shuffles_sec,
            )
        except ValueError as error:
            await bot.discord.send(
                str(error),
                response=True,
                ephemeral=True,
            )
            return

        await bot.discord.send(
            view=view,
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
