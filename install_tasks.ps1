# Register (or refresh) the two SEU-ISP auto-login scheduled tasks.
# Run as Administrator: double-click install_tasks.cmd, or run this file from an elevated PowerShell.
#
#   SEU-ISP-AutoLogin-Boot : at system startup, runs as SYSTEM (works at the lock screen)
#   SEU-ISP-AutoLogin      : at user logon and every 15 minutes, runs as the current user
#
# It also turns off the Windows "sign in to network" popup (see signin_popup.ps1).
# Pass -KeepSignInPopup to skip that step.
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads non-BOM UTF-8 as ANSI and would garble text.

param([switch]$KeepSignInPopup)

$ErrorActionPreference = 'Stop'
$failed = $false

$base = $PSScriptRoot
$loginScript = Join-Path $base 'seu_isp_login.py'
$config = Join-Path $base 'config.json'
$consoleExe = Join-Path $base 'seu-autologin.exe'
$windowExe = Join-Path $base 'seu-autologinw.exe'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'ERROR: please run as Administrator.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $config)) {
    Write-Host "ERROR: $config not found. Copy config.example.json to config.json and fill it in first." -ForegroundColor Red
    exit 1
}

# Pick the runner: bundled exe first (no console window for the task), then pythonw + script.
$bootExecute = $null
$logonExecute = $null
$bootArgument = ''
$logonArgument = ''
if (Test-Path -LiteralPath $consoleExe) {
    $bootExecute = $consoleExe
}
if (Test-Path -LiteralPath $windowExe) {
    $logonExecute = $windowExe
}
if (-not $bootExecute) {
    if (-not (Test-Path -LiteralPath $loginScript)) {
        Write-Host "ERROR: neither seu-autologin.exe nor seu_isp_login.py found in $base." -ForegroundColor Red
        exit 1
    }
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) {
        Write-Host 'ERROR: seu-autologin.exe not found and pythonw.exe is not in PATH.' -ForegroundColor Red
        exit 1
    }
    $bootExecute = $logonExecute = $pythonw
    $bootArgument = $logonArgument = ('-S "{0}"' -f $loginScript)
}
Write-Host ('Runner (boot)  : ' + $bootExecute)
Write-Host ('Runner (logon) : ' + $logonExecute)

if ($bootArgument) {
    $action = New-ScheduledTaskAction -Execute $bootExecute -Argument $bootArgument -WorkingDirectory $base
} else {
    # 注意：-Argument 不接受空字符串，用 exe 当 runner 时要省略这个参数
    $action = New-ScheduledTaskAction -Execute $bootExecute -WorkingDirectory $base
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -Hidden
$description = 'SEU-ISP auto login'

$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$bootPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
try {
    Register-ScheduledTask -TaskName 'SEU-ISP-AutoLogin-Boot' -Action $action -Trigger $bootTrigger -Settings $settings -Principal $bootPrincipal -Description $description -Force -ErrorAction Stop | Out-Null
    Write-Host 'OK   SEU-ISP-AutoLogin-Boot   (at system startup, SYSTEM, works at the lock screen)'
} catch {
    Write-Host ('FAILED SEU-ISP-AutoLogin-Boot : ' + $_.Exception.Message) -ForegroundColor Red
    $failed = $true
}

if ($logonExecute -ne $bootExecute) {
    if ($logonArgument) {
        $action = New-ScheduledTaskAction -Execute $logonExecute -Argument $logonArgument -WorkingDirectory $base
    } else {
        $action = New-ScheduledTaskAction -Execute $logonExecute -WorkingDirectory $base
    }
}
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes 15)

# Extra trigger: right after the network changes (WiFi switch / reconnect). 5 second delay so
# DHCP/DNS settle a bit first. Requires the NetworkProfile operational log (enabled by default).
$subscription = '<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[Provider[@Name=''Microsoft-Windows-NetworkProfile''] and EventID=10000]]</Select></Query></QueryList>'
$eventTrigger = New-CimInstance -CimClass (Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskEventTrigger) -ClientOnly
$eventTrigger.Enabled = $true
$eventTrigger.Subscription = $subscription
$eventTrigger.Delay = 'PT5S'

$userPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
try {
    Register-ScheduledTask -TaskName 'SEU-ISP-AutoLogin' -Action $action -Trigger $logonTrigger, $repeatTrigger, $eventTrigger -Settings $settings -Principal $userPrincipal -Description $description -Force -ErrorAction Stop | Out-Null
    Write-Host 'OK   SEU-ISP-AutoLogin        (at logon + every 15 min + right after the network changes)'
} catch {
    Write-Host ('FAILED SEU-ISP-AutoLogin : ' + $_.Exception.Message) -ForegroundColor Red
    $failed = $true
}

Write-Host ''
Write-Host ('Log file : ' + (Join-Path $base 'login.log'))
Write-Host ('Test now : ' + $logonExecute + ' --once')
Write-Host 'Run task : Start-ScheduledTask -TaskName SEU-ISP-AutoLogin'
Write-Host 'Popup    : 3-....cmd turns it off, 4-....cmd turns it back on'
Write-Host 'Remove   : Unregister-ScheduledTask -TaskName SEU-ISP-AutoLogin -Confirm:$false'
Write-Host '           Unregister-ScheduledTask -TaskName SEU-ISP-AutoLogin-Boot -Confirm:$false'

# Windows pops a "sign in to network" notification (and opens the browser) every time it
# joins SEU-ISP / SEU-WLAN, because the campus portal hijacks Windows' own connectivity
# probe. Our login is seconds faster than the popup is useful, so turn the probe off.
Write-Host ''
$popupScript = Join-Path $base 'signin_popup.ps1'
if ($KeepSignInPopup) {
    Write-Host 'SKIP  sign-in popup left enabled (-KeepSignInPopup)'
} elseif (-not (Test-Path -LiteralPath $popupScript)) {
    Write-Host 'SKIP  signin_popup.ps1 not found next to install_tasks.ps1' -ForegroundColor Yellow
} else {
    & $popupScript
}

if ($failed) { exit 1 }
