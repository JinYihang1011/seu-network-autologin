@echo off
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code page,
rem so non-ASCII text here would be garbled on a Chinese Windows.
rem Turns OFF the Windows "sign in to network" popup (NCSI active probing).
rem The installer (2-....cmd) already does this; use this file to re-apply it.
setlocal
set "PSEXE=powershell.exe"
where pwsh.exe >nul 2>nul && set "PSEXE=pwsh.exe"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PSEXE%' -Verb RunAs -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0signin_popup.ps1'"
