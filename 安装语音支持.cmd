@echo off
chcp 65001 >nul
call "%~dp0安装-UniCampusHelper.cmd" --audio
exit /b %errorlevel%
