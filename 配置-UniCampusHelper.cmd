@echo off
chcp 65001 >nul
call "%~dp0启动-UniCampusHelper.cmd" --settings
exit /b %errorlevel%
