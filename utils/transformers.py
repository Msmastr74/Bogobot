import discord

class ColourTransformer(discord.app_commands.Transformer):
    async def transform(self, interaction: discord.Interaction, value: str) -> discord.Colour:
        value = value.strip().lower()
        hex_value = value.lstrip('#').lstrip('0x')
        try:
            return discord.Colour(int(hex_value, 16))
        except ValueError:
            allowed_props = {'ash_embed', 'ash_theme', 'blue', 'blurple', 'brand_green', 'brand_red', 'dark_blue', 'dark_embed', 'dark_gold', 'dark_gray', 'dark_green', 'dark_grey', 'dark_magenta', 'dark_orange', 'dark_purple', 'dark_red', 'dark_teal', 'dark_theme', 'darker_gray', 'darker_grey', 'default', 'fuchsia', 'gold', 'green', 'greyple', 'light_embed', 'light_gray', 'light_grey', 'light_theme', 'lighter_gray', 'lighter_grey', 'magenta', 'og_blurple', 'onyx_embed', 'onyx_theme', 'orange', 'pink', 'purple', 'random', 'red', 'teal', 'yellow'}
            if value in allowed_props and hasattr(discord.Colour, value):
                return getattr(discord.Colour, value)()
            raise discord.app_commands.TransformerError(
                value, self.type, self
            )

