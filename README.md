# Bogobot
Discord bot code made for monitoring swapjs's 24/7 bogosort Livestream

## Setup
The official Bogobot is privated to save my phone's RAM and CPU. So you will have to host the bot yourself in order to use it, which is why the code is open source. Here are the setup instructions:

1. Install the python terminal runtime
2. run pip install git (or pkg install git if you're on termux)
3. run git clone https://github.com/Msmastr74/Bogobot
4. configure config.json 
5. cd into the Bogobot folder it made
6. run bash dependecies-windows.sh (or bash dependencies-termux.sh if you're on termux)
7. in a separate terminal tab, run streamlink https://www.youtube.com/live/vzgH2DGhrUA 720p --stdout | ffmpeg -re -i pipe:0 -vf "fps=1" -update 1 -y live_720p.jpg
8. run python main.py and the bot should be running

## API
Bogobot is built using a core API which handles all the complex bot stuff for you and has a bunch of functions and organized classes to make things easy for you. Scripts using this API are put in plugins, which is loaded by main.py with the full core to then run the bot. Bogobot comes pre-loaded with a handful of base plugins that have the bots basic functionality (as seen in the original bot). The docs for the core API are below.

### bot Class
Contains all the classes mentioned within the doc, handles all the bot's functionality.

### info Class
Gets info from the stream.
funcs: get_last_shuffle, get_all_stats (gets a dictionary containing values for all the stats you see on stream)

### discord Class
Handles message sending, editing, and deleting.
subclasses: messages, embeds
funcs: messages.send, messages.edit, messages.delete, embeds.send, embeds.edit embeds.delete

### setup Class
Handles setop for the script.
funcs: command, channel_id
