# Build the standalone Windows executables with PyInstaller.
#
#   pip install pyinstaller
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Output: dist\seu-autologin.exe   (console build, for manual runs)
#         dist\seu-autologinw.exe  (windowed build, for scheduled tasks)
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads non-BOM UTF-8 as ANSI.
param(
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$base = $PSScriptRoot
$source = Join-Path $base 'seu_isp_login.py'

& $Python -m PyInstaller --onefile --clean --noconfirm --name seu-autologin `
    --distpath (Join-Path $base 'dist') --workpath (Join-Path $base 'build') `
    --specpath (Join-Path $base 'build') $source

& $Python -m PyInstaller --onefile --noconsole --clean --noconfirm --name seu-autologinw `
    --distpath (Join-Path $base 'dist') --workpath (Join-Path $base 'build') `
    --specpath (Join-Path $base 'build') $source

Write-Host ''
Get-ChildItem (Join-Path $base 'dist') | Select-Object Name, @{ n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }
