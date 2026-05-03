# Bogobot Internal API Documentation

Bogobot is built on a modular architecture that separates core logic from command implementation. This documentation outlines the internal classes and methods available for extending the bot's functionality.

## BotCore Class
The `BotCore` class is the central manager for the bot, handling configuration, command tree synchronization, and plugin loading. It inherits from `discord.Client`.

### Configuration
Configuration is managed via `config.json`. Key fields include:
- `token`: The Discord bot token.
- `authorized_users`: A list of user IDs with elevated permissions.
- `sync`: Whether to sync the command tree on the next run. This parameter is automatically reset to false each run.
- `ocr_debug`: Enable/disable saving ocr debug images.

## Info Subclass
The `info` subclass handles data extraction from the livestream using a combination of OCR and API requests.

### Methods
* `get_stats_all()`: Returns a dictionary containing the most recent values for shuffles, comparisons, and calculated shuffles per minute.
* `get_uptime()`: An asynchronous method that fetches the raw epoch timestamp from the YouTube Framework Update API and returns a formatted `DD:HH:MM:SS` string.
* `format_to_ddhhmmss(total_seconds)`: Converts raw seconds into a standardized duration string.

## Discord Subclass
The `discord` subclass provides a simplified interface for interacting with the Discord API, specifically designed for use within plugins.

### Messages
* `messages.send(contents, response=True)`: Sends a plain text message. If `response` is true, it attempts to reply to the current interaction.

### Embeds
* `embeds.send(contents, title, color, footer, response=True)`: Initializes and sends a new embed.
* `embeds.edit(contents, title, author, add_field=False)`: Modifies the existing active embed. Setting `add_field` to true will append a new field instead of editing the main body.
* `embeds.delete()`: Removes the active embed from the channel.

## OCR Implementation
Bogobot utilizes Tesseract OCR for visual data extraction.
- **Coordinates**: Stats are extracted from defined regions of a 720p frame.
- **Processing**: Frames are pre-processed using the Pillow (PIL) library, including grayscale conversion and thresholding, to improve recognition accuracy.
- **Whitelist**: A strict digit-only whitelist is enforced to prevent formatting errors from phantom characters or background noise.

## Plugin System
Plugins are independent Python files located in the `/plugins` directory.

### Creating a Plugin
Each plugin must include a `setup` function to register commands with the `BotCore` instance:

```python
import discord

async def setup(bot):
    @bot.setup.command(name="example", description="An example command", perm_requirement=0)
    async def example(interaction: discord.Interaction):
        await bot.discord.messages.send("Hello World", response=True)

```
### Permission Levels
 * **0 (Public)**: Accessible by all users.
 * **1 (Authorized)**: Accessible by users listed in the authorized_users configuration.
 * **2 (Admin)**: Restricted to administrative tasks.
