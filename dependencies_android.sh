#!/bin/bash
# For use within the Termux environment

# Update and upgrade Termux packages
pkg update && pkg upgrade -y

# Install core binaries
# Termux provides specific builds for tesseract and ffmpeg
pkg install python tesseract ffmpeg -y

#untested ---
pkg install x11-repo
pkg install opencv-python
# ---

# Install Python libraries
# Pillow may require a moment to compile on mobile devices
pip install discord.py numpy Pillow requests streamlink
