#!/bin/bash
# Update system repositories
sudo apt update && sudo apt upgrade -y

# Install system binaries
# python3-pip for package management
# tesseract-ocr for image processing
# ffmpeg for stream piping
sudo apt install python3 python3-pip tesseract-ocr ffmpeg -y

# Install Python libraries
# discord.py: Bot API
# numpy & Pillow: Image manipulation for OCR
# aiohttp: YouTube API communication (also a dependency of discord.py, just explicitly included here)
# streamlink: Livestream data extraction
pip3 install discord.py numpy Pillow aiohttp streamlink opencv-python
