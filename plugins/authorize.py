import discord
from discord.ext import tasks

async def setup(bot):
    @bot.setup.command(name="authorize", description="Add a user to the authorization list", perm_requirement=2)
    async def authorize(interaction: discord.Interaction, user: discord.Member):
        if user.id not in bot.config["authorized_users"]:
            bot.config["authorized_users"].append(user.id)
            bot.save_config()
            return await interaction.response.send_message(f"<:Sorted:1495837069996720249> User {user.display_name} has been authorized", ephemeral=True)
        else:
            return await interaction.response.send_message(f"<:Unsorted:1495837051235598346> User {user.display_name} is already authorized", ephemeral=True)
    @bot.setup.command(name="deauthorize", description="Revokes a user's authorization", perm_requirement=2)
    async def deauthorize(interaction: discord.Interaction, user: discord.Member):
        if user.id in bot.config["authorized_users"]:
            bot.config["authorized_users"].remove(user.id)
            bot.save_config()
            return await interaction.response.send_message(f"<:Sorted:1495837069996720249> User {user.display_name} has been deauthorized", ephemeral=True)
        else:
            return await interaction.response.send_message(f"<:Unsorted:1495837051235598346> User {user.display_name} isnt authorized",ephemeral=True)