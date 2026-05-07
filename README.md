# Bogobot

Bogobot is a specialized Discord bot designed for monitoring the [24/7 Bogosort Livestream](https://www.youtube.com/live/DgfiqGPmGWY). The bot utilizes a hybrid of Optical Character Recognition (OCR) and YouTube Framework Metadata to provide high-accuracy statistics directly from the stream.

## Prerequisites

The only thing required is Python 3.10+. Everything else is installed in the dependency file

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Msmastr74/Bogobot
   cd Bogobot
   ```
2. Run the dependency script for your environment:
   ```bash
   bash dependencies_android.sh   # For Android/Termux
   # OR
   bash dependencies_windows.sh  # For Windows
   # OR
   bash dependencies_linux.sh  # For Linux
   ```
## Configuration
Go into config.json and provide the following credentials:
 * token: Discord Bot Token.
 * owner_UID: Your user ID
 * owner_username: Your username

## Execution
### Terminal 1: Bot Core
```bash
python main.py
```
## Features
Bogobot implements several slash commands for stream management and data retrieval:
 * /get_stats: Retrieves current shuffles, comparisons, and calculated uptime.
 * /monitor: Initiates a persistent tracking system for stream serial numbers.
 * /roll: A random number generation utility.
 * /authorize: Manages user permissions.
## Documentation
For technical details regarding the internal API, OCR configuration, and plugin development, refer to DOCS.md.
