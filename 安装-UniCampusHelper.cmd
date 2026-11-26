@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
if not exist "scripts\install_windows.py" goto incomplete
if exist ".runtime\pythonw.exe" if /i "%~1" neq "--audio" goto portable
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=0"
set "check_python=import sys, platform; sys.exit(not (sys.version_info >= (3,11) and sys.maxsize.bit_length() == 63 and platform.machine().lower() in ('amd64','x86_64')))"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -B -c "%check_python%" >nul 2>&1
    if not errorlevel 1 goto existing
)
where /q py.exe
if not errorlevel 1 (
    for %%v in (3.13 3.12 3.14 3.11) do (
        py.exe -%%v -B -c "%check_python%" >nul 2>&1
        if not errorlevel 1 (
            set "py_version=%%v"
            goto launcher
        )
    )
)
where /q python.exe
if not errorlevel 1 (
    python.exe -B -c "%check_python%" >nul 2>&1
    if not errorlevel 1 goto system
)
echo 未找到可用的 64 位 Python。源码安装和语音支持需要 Python 3.11 或更新版本，推荐 3.12 或 3.13。
echo Python 下载页 https://www.python.org/downloads/windows/
echo 基础功能可直接下载 Windows 免安装包 https://github.com/zhuangzztong-bit/UniCampusHelper/releases/latest
goto failed

:existing
".venv\Scripts\python.exe" -B -X utf8 "scripts\install_windows.py" %*
goto result

:launcher
py.exe -%py_version% -B -X utf8 "scripts\install_windows.py" %*
goto result

:system
python.exe -B -X utf8 "scripts\install_windows.py" %*
goto result

:portable
call "%~dp0启动-UniCampusHelper.cmd" %*

:result
if errorlevel 1 goto failed
popd
exit /b 0

:incomplete
echo 请先将下载的 ZIP 完整解压，再运行安装入口。

:failed
pause
popd
exit /b 1
