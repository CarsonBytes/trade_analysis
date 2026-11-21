# ONE-TIME setup for the IBKR passkey helper. Run it yourself, AT THE PC, in an ELEVATED PowerShell
# ("Run as administrator"):
#     powershell -ExecutionPolicy Bypass -File D:\quant\scripts\windows\setup-passkey-helper.ps1
#
# It (1) registers the scheduled task "\IBKR\Passkey Helper" (your account, Run with highest
# privileges, only when you are logged on -- so it can see and type into the Windows Security
# dialog), and (2) asks YOU to type the YubiKey PIN, which is stored in Windows Credential Manager
# as "IBKR-YubiKey-PIN" (encrypted, readable only by your Windows account). The PIN is never shown,
# printed or written to any file.
$ErrorActionPreference = 'Stop'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "Please run this in an ELEVATED PowerShell (Run as administrator)." -ForegroundColor Red; exit 1 }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wscript.exe" -Argument '"D:\quant\scripts\windows\ibkr-passkey-helper.vbs"'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskPath '\IBKR\' -TaskName 'Passkey Helper' -Action $action -Principal $principal -Settings $settings `
    -Description 'Selects Security key and enters the YubiKey PIN in the IBKR passkey dialog. Triggered only by ibkr-live-login.ps1.' -Force | Out-Null
Write-Host "Registered task \IBKR\Passkey Helper (runs as $user, highest privileges, on demand only)." -ForegroundColor Green

Write-Host ""
Write-Host "Now store the YubiKey PIN. cmdkey will ask for it (typing is hidden):" -ForegroundColor Cyan
cmdkey /generic:IBKR-YubiKey-PIN /user:yubikey /pass
if ($LASTEXITCODE -eq 0) { Write-Host "PIN stored in Credential Manager as IBKR-YubiKey-PIN." -ForegroundColor Green }
else { Write-Host "Storing the PIN failed -- re-run this script." -ForegroundColor Red; exit 1 }
Write-Host ""
Write-Host "Done. From now on 'IBKR Live Login' does everything except touching the key." -ForegroundColor Green
