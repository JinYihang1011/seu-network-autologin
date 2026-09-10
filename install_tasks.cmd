@echo off
setlocal
set "PSEXE=powershell.exe"
where pwsh.exe >nul 2>nul && set "PSEXE=pwsh.exe"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PSEXE%' -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0install_tasks.ps1'"
