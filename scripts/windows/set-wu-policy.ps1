# One-time elevated task body: set the Windows Update no-auto-reboot policy (MIN-2FA P2).
# Each forced WU reboot kills the gateway before its healthy 20:30 shutdown, so the next
# start demands a full passkey -- 3 such reboots happened Oct 13-15 alone. Read-back is
# logged so the result is verifiable without an admin shell.
$Log = 'C:\IBC-Live\Logs\wu-policy.log'
function Log($m) { "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m | Add-Content -Path $Log -Encoding UTF8 }

Log "=== applying Windows Update policy ==="
$k = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
try {
    New-Item -Path $k -Force | Out-Null
    New-ItemProperty -Path $k -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1 -PropertyType DWord -Force | Out-Null
    $v = (Get-ItemProperty -Path $k -Name 'NoAutoRebootWithLoggedOnUsers' -ErrorAction Stop).NoAutoRebootWithLoggedOnUsers
    if ($v -eq 1) { Log "OK: NoAutoRebootWithLoggedOnUsers=1 confirmed at $k" }
    else { Log "FAIL: read-back returned '$v', expected 1" }
} catch {
    Log "FAIL: $($_.Exception.Message)"
}
