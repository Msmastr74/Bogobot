import discord
from discord import app_commands

class ColourTransformer(app_commands.Transformer):
    predefined_colours = {'ash_embed', 'ash_theme', 'blue', 'blurple', 'brand_green', 'brand_red', 'dark_blue', 'dark_embed', 'dark_gold', 'dark_gray', 'dark_green', 'dark_grey', 'dark_magenta', 'dark_orange', 'dark_purple', 'dark_red', 'dark_teal', 'dark_theme', 'darker_gray', 'darker_grey', 'default', 'fuchsia', 'gold', 'green', 'greyple', 'light_embed', 'light_gray', 'light_grey', 'light_theme', 'lighter_gray', 'lighter_grey', 'magenta', 'og_blurple', 'onyx_embed', 'onyx_theme', 'orange', 'pink', 'purple', 'random', 'red', 'teal', 'yellow'}
    async def transform(self, interaction: discord.Interaction, value: str) -> discord.Colour:
        value = value.strip().lower()
        hex_value = value.lstrip('#').lstrip('0x')
        try:
            return discord.Colour(int(hex_value, 16))
        except ValueError:
            if value in self.predefined_colours and hasattr(discord.Colour, value):
                return getattr(discord.Colour, value)()
            raise app_commands.TransformerError(
                value, self.type, self
            )
    
    async def autocomplete(self, interaction: discord.Interaction, value: str) -> list[app_commands.Choice[str]]: # pyright: ignore
        value = value.strip().lower()
        
        choices: list[app_commands.Choice[str]] = []
        try:
            int(value.lstrip('#').lstrip('0x'), 16)
            choices.insert(0, app_commands.Choice(name=value, value=value))
        except ValueError:
            pass
        
        for colour in self.predefined_colours:
            if colour.startswith(value):
                choices.append(app_commands.Choice(name=colour, value=colour))
        return choices[:25]

class IntTransformer(app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: str) -> int:
        try:
            return int(value)
        except ValueError:
            raise app_commands.TransformerError(
                value, self.type, self
            )
