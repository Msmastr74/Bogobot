# Stop executing if any command fails (equivalent to 'set -e')
$ErrorActionPreference = "Stop"

# Ensure the script runs with Administrator privileges
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "Please run this PowerShell console as an Administrator!"
    Exit
}

# Install system binaries via WinGet
Write-Host "Installing system binaries..." -ForegroundColor Cyan
winget install -e --id Python.Python.3.12 --silent
winget install -e --id UB-Mannheim.TesseractOCR --silent
winget install -e --id Gyan.FFmpeg --silent
winget install -e --id BytecodeAlliance.Wasmtime --silent

# Force refresh environment variables so Python and newly installed tools are immediately available
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# Install Python libraries
Write-Host "Installing Python libraries..." -ForegroundColor Cyan
py -3 -m pip install --upgrade pip
py -3 -m pip install discord.py openai numpy Pillow aiohttp streamlink opencv-python pyuca pytchat google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2

# Verify libtesseract can be loaded by Python's ctypes path
Write-Host "Verifying libtesseract load..." -ForegroundColor Cyan
$pythonCode = @'
import ctypes
import ctypes.util
import os
import sys

candidate_paths = [
    ctypes.util.find_library("tesseract"),
    "libtesseract-5.dll",
    "libtesseract-4.dll",
    "libtesseract.dll",
    os.path.join(os.environ.get("ProgramFiles", ""), "Tesseract-OCR", "libtesseract-5.dll"),
    os.path.join(os.environ.get("ProgramFiles", ""), "Tesseract-OCR", "libtesseract-4.dll"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tesseract-OCR", "libtesseract-5.dll"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tesseract-OCR", "libtesseract-4.dll"),
]

errors = []
dll_directories = []
for path in candidate_paths:
    if not path:
        continue
    try:
        directory = os.path.dirname(path)
        if directory and os.path.isdir(directory) and hasattr(os, "add_dll_directory"):
            dll_directories.append(os.add_dll_directory(directory))
        ctypes.CDLL(path)
        print(f"libtesseract load check passed: {path}")
        sys.exit(0)
    except OSError as e:
        errors.append(f"{path}: {e}")

sys.exit("Could not load libtesseract. Tried: " + "; ".join(errors))
'@

# Execute the inline Python code
$pythonCode | py -3

Write-Host "If streamlink, ffmpeg, or tesseract are still not found in this window, please open a fresh PowerShell terminal." -ForegroundColor Yellow
