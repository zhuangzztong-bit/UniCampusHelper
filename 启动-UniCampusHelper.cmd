@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "UnipusAI_Helper.py"
) else if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\python.exe" "UnipusAI_Helper.py"
) else (
    start "" pythonw "UnipusAI_Helper.py"
)
