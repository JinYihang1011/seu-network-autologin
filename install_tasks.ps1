# Register (or refresh) the two SEU-ISP auto-login scheduled tasks.
# Run as Administrator: double-click install_tasks.cmd, or run this file from an elevated PowerShell.
#
#   SEU-ISP-AutoLogin-Boot : at system startup, runs as SYSTEM (works at the lock screen)
#   SEU-ISP-AutoLogin      : at user logon and every 15 minutes, runs as the current user
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads non-BOM UTF-8 as ANSI and would garble text.

$ErrorActionPreference = 'Stop'

$base = $PSScriptRoot
$loginScript = Join-Path $base 'seu_isp_login.py'
$config = Join-Path $base 'config.json'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'ERROR: please run as Administrator.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $loginScript)) {
    Write-Host "ERROR: $loginScript not found." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $config)) {
    Write-Host "ERROR: $config not found. Copy config.example.json to config.json and fill it in first." -ForegroundColor Red
    exit 1
}

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    Write-Host 'ERROR: pythonw.exe not found in PATH. Install Python 3 first.' -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('-S "{0}"' -f $loginScript) -WorkingDirectory $base
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -Hidden
$description = 'SEU-ISP auto login'

$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$bootPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'SEU-ISP-AutoLogin-Boot' -Action $action -Trigger $bootTrigger -Settings $settings -Principal $bootPrincipal -Description $description -Force | Out-Null
Write-Host 'OK   SEU-ISP-AutoLogin-Boot   (at system startup, SYSTEM, works at the lock screen)'

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15)
$userPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'SEU-ISP-AutoLogin' -Action $action -Trigger $logonTrigger, $repeatTrigger -Settings $settings -Principal $userPrincipal -Description $description -Force | Out-Null
Write-Host 'OK   SEU-ISP-AutoLogin        (at user logon + every 15 minutes)'

Write-Host ''
Write-Host ('Log file : ' + (Join-Path $base 'login.log'))
Write-Host 'Test now : python seu_isp_login.py --once'
Write-Host 'Remove   : Unregister-ScheduledTask -TaskName SEU-ISP-AutoLogin -Confirm:$false'
Write-Host '           Unregister-ScheduledTask -TaskName SEU-ISP-AutoLogin-Boot -Confirm:$false'
