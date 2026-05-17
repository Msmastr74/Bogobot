from bogobot_core import BotCore


def manage(bot: BotCore):
    return bot.setup.group("manage", "Bot management commands")

def accounts(bot: BotCore):
    return bot.setup.group("accounts", "Account management commands")
