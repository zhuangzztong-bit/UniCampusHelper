@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
if not exist "scripts\launch_workbench.py" goto incomplete
if exist ".runtime\pythonw.exe" if not exist ".venv\.ucampus-ready" goto portable
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -B "scripts\launch_workbench.py" %*
) else if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\python.exe" -B "scripts\launch_workbench.py" %*
) else (
    if exist ".runtime\pythonw.exe" goto portable
    call "%~dp0安装-UniCampusHelper.cmd" %*
)
if errorlevel 1 goto failed
goto done

:portable
start "" ".runtime\pythonw.exe" -B "scripts\launch_workbench.py" %*
if errorlevel 1 goto failed
goto done

:incomplete
echo 请将下载的 ZIP 全部解压到可写的文件夹，再从解压后的目录运行此文件。
pause
:failed
popd
exit /b 1

:done
popd
exit /b 0
