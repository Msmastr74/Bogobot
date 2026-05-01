import discord
async def info_commands(bot):
    @bot.setup.command(name="avatar", description="Get the avatar of a user", eph=False, perm_requirement=0)
    async def avatar(inter: discord.Interaction, user: discord.Member = None):
        if user is None:
            user = inter.user
        embed = discord.Embed(title=f"{user.display_name}'s Avatar:")
        embed.set_image(url=user.avatar.url)

        await inter.response.send_message(embed=embed)
