@echo off
cd /d "%~dp0"
start "" pythonw bridge_gui.py
if errorlevel 1 (
  echo pythonw not found, trying python ...
  python bridge_gui.py
  pause
)
