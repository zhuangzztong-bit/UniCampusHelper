@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -B "scripts\launch_workbench.py"
) else if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\python.exe" -B "scripts\launch_workbench.py"
) else (
    where /q pythonw.exe
    if errorlevel 1 (
        where /q pyw.exe
        if errorlevel 1 (
            echo 未找到 Python。请安装 Python 3.12 或 3.13，并按 README 创建 .venv。
            pause
            exit /b 1
        )
        start "" pyw.exe -3 -B "scripts\launch_workbench.py"
    ) else (
        start "" pythonw.exe -B "scripts\launch_workbench.py"
    )
)
