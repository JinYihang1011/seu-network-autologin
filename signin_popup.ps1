# Turn the Windows "sign in to network" popup OFF (default) or back ON (-Restore).
#
# Why the popup appears: every time Windows joins SEU-ISP / SEU-WLAN, its Network
# Connectivity Status Indicator (NCSI) probes http://www.msftconnecttest.com/connecttest.txt
# to decide whether the network has internet. The campus portal hijacks that probe, so
# Windows concludes "this hotspot needs a sign-in page" and shows the "Sign in to network"
# notification, then opens your browser at the portal. This tool authenticates about a
# second later, so the popup is pure noise.
#
# What this does: sets EnableActiveProbing = 0 in
#   HKLM\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet
# and restarts the Network Location Awareness service so it takes effect immediately.
# This tool checks the internet itself by requesting the very same URL, so login
# correctness is unaffected.
#
# Cost: Windows no longer knows a network needs sign-in, so it will not warn you. The
# tray icon may look "connected" while you are actually not authenticated - check login.log.
#
# Usage:  powershell -ExecutionPolicy Bypass -File signin_popup.ps1            (turn off)
#         powershell -ExecutionPolicy Bypass -File signin_popup.ps1 -Restore   (turn back on)
# Easier: double-click 3-关闭联网验证弹窗.cmd / 4-恢复联网验证弹窗.cmd
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads non-BOM UTF-8 as ANSI and would garble text.

param([switch]$Restore)

$ErrorActionPreference = 'Stop'
$key = 'HKLM:\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet'
$keyPath = 'HKLM\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet'
$target = if ($Restore) { 1 } else { 0 }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'ERROR: please run as Administrator.' -ForegroundColor Red
    Write-Host 'Tip: double-click 3-....cmd / 4-....cmd instead - they ask for elevation for you.'
    return
}

$before = (Get-ItemProperty -Path $key -Name EnableActiveProbing -ErrorAction SilentlyContinue).EnableActiveProbing
if ($null -eq $before) {
    Write-Host ('WARN: EnableActiveProbing not found at ' + $key + ' (unexpected).') -ForegroundColor Yellow
}

Set-ItemProperty -Path $key -Name EnableActiveProbing -Value $target -Type DWord
$after = (Get-ItemProperty -Path $key -Name EnableActiveProbing).EnableActiveProbing

try {
    Restart-Service -Name NlaSvc -Force -ErrorAction Stop
    Start-Sleep -Seconds 2
    $svc = (Get-Service -Name NlaSvc).Status
} catch {
    $svc = 'restart failed: ' + $_.Exception.Message
}

Write-Host ''
if ($Restore) {
    Write-Host 'Windows NCSI active probing: back ON. The "sign in to network" popup will come back.' -ForegroundColor Yellow
} else {
    Write-Host 'Windows NCSI active probing: OFF. The "sign in to network" popup is disabled.' -ForegroundColor Green
}
Write-Host ('  EnableActiveProbing : {0} -> {1}' -f $before, $after)
Write-Host ('  NlaSvc status       : ' + $svc)
Write-Host ''
Write-Host 'No reboot needed.'
if ($Restore) {
    Write-Host 'To disable the popup again: run signin_popup.ps1 (or 3-....cmd).'
} else {
    Write-Host 'The tool still verifies the internet itself, so login correctness is unchanged.'
    Write-Host 'To bring the popup back: run signin_popup.ps1 -Restore (or 4-....cmd),'
    Write-Host ('or manually: reg add "' + $keyPath + '" /v EnableActiveProbing /t REG_DWORD /d 1 /f') 
}
Write-Host ''
Write-Host 'You can close this window now.'
