@echo off
title TCG Software Launcher
powershell.exe -ExecutionPolicy RemoteSigned -NoProfile -File "%~dp0scripts\launch.ps1"
if %ERRORLEVEL% neq 0 (
    echo.
    echo Something went wrong. Read the messages above for details.
    echo.
    pause
)
