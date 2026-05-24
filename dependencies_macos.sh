#!/bin/bash
# For macOS using Homebrew
set -e

# Install Homebrew first if needed:
# https://brew.sh

# Update Homebrew formulae
brew update

# Install system binaries
# python: Python 3 and pip
# tesseract: OCR engine and libtesseract shared library
# ffmpeg: stream piping
# streamlink: livestream extraction
brew install python tesseract ffmpeg streamlink

# Install Python libraries
# discord.py: Bot API
# torch/transformers: local NL routing and function calling
# numpy & Pillow: Image manipulation for OCR
# aiohttp: YouTube API communication (also a dependency of discord.py, just explicitly included here)
# opencv-python: image preprocessing
python3 -m pip install --upgrade pip
python3 -m pip install discord.py torch transformers numpy Pillow aiohttp opencv-python pyuca bitsandbytes accelerate

# Verify libtesseract can be loaded by Python's ctypes path.
python3 - <<'PY'
import ctypes
import ctypes.util

path = ctypes.util.find_library("tesseract") or "libtesseract.dylib"
ctypes.CDLL(path)
print(f"libtesseract load check passed: {path}")
PY
