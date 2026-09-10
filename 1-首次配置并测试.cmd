@echo off
rem Keep this file ASCII-only: cmd.exe parses batch files with the console code page,
rem so non-ASCII text here would be garbled on a Chinese Windows. All Chinese output
rem comes from the program itself (Python handles the console encoding correctly).
cd /d "%~dp0"
if exist "seu-autologin.exe" (
    "seu-autologin.exe" --once --pause
) else if exist "seu_isp_login.py" (
    python "seu_isp_login.py" --once --pause
) else (
    echo [ERROR] seu-autologin.exe / seu_isp_login.py not found in this folder.
    pause
)
