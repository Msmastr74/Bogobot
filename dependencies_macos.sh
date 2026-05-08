#!/bin/bash
# For macOS using Homebrew

# Install Homebrew first if needed:
# https://brew.sh

# Update Homebrew formulae
brew update

# Install system binaries
# python: Python 3 and pip
# tesseract: OCR engine
# ffmpeg: stream piping
# streamlink: livestream extraction
brew install python tesseract ffmpeg streamlink

# Install Python libraries
# discord.py: Bot API
# numpy & Pillow: Image manipulation for OCR
# aiohttp: YouTube API communication (also a dependency of discord.py, just explicitly included here)
# opencv-python: image preprocessing
python3 -m pip install discord.py numpy Pillow aiohttp opencv-python
