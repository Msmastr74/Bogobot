#!/bin/bash
set -e

# Update system repositories
sudo apt update && sudo apt upgrade -y

# Install system binaries
# python3-pip for package management
# tesseract-ocr/libtesseract-dev for libtesseract OCR
# ffmpeg for stream piping
sudo apt install python3 python3-pip tesseract-ocr libtesseract-dev ffmpeg -y

curl https://wasmtime.dev/install.sh -sSf | bash

# Install Python libraries
# discord.py: Bot API
# openai: hosted or OpenAI-compatible AI action selection and replies
# numpy, Pillow & opencv-python: Image manipulation for OCR
# aiohttp: YouTube API communication (also a dependency of discord.py, just explicitly included here)
# streamlink: Livestream data extraction
python3 -m pip install --upgrade pip
python3 -m pip install discord.py openai numpy Pillow aiohttp streamlink opencv-python pyuca pytchat

# Verify libtesseract can be loaded by Python's ctypes path.
python3 - <<'PY'
import ctypes
import ctypes.util

path = ctypes.util.find_library("tesseract") or "libtesseract.so"
ctypes.CDLL(path)
print(f"libtesseract load check passed: {path}")
PY
