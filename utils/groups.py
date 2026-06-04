from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from bogobot_core import BotCore

def manage(bot: 'BotCore'):
    return bot.setup.group("manage", "Bot management commands")

def accounts(bot: 'BotCore'):
    return bot.setup.group("accounts", "Account management commands")

def bogo(bot: 'BotCore'):
    return bot.setup.group("bogo", "Fun /bogo commands")

def ai_activity(bot: 'BotCore'):
    return bot.setup.group("ai_activity", "AI activity controls")

def archive(bot: 'BotCore'):
    return bot.setup.group("archive", "Archive commands")
