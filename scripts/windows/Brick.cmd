@echo off
rem TrimUI Brick emulator - double-click to start. Arguments are passed to Brick.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Brick.ps1" %*
if errorlevel 1 pause
