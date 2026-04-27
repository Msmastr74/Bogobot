# This script uses winget (Windows Package Manager) to install binaries
# Ensure you run this in an Administrative terminal

# Install Binaries
winget install -e --id Python.Python.3.12
winget install -e --id UB-Mannheim.TesseractOCR
winget install -e --id gyan.ffmpeg

# Install Python libraries
pip install discord.py numpy Pillow requests streamlink

# Note: Manual verification of PATH environment variables for 
# Tesseract and FFmpeg is recommended after installation.
