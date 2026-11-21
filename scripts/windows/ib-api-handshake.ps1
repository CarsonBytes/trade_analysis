# IB API handshake probe (PowerShell port of tmp/ib_hs2.py).
# Dot-source this file, then call: Test-IbApiHandshake [-Host 127.0.0.1] [-Port 4001] [-TimeoutSec 6]
# Returns $true iff the server replies with a valid length-prefixed IB API version field.
# A bare TCP listener that accepts and closes (or an unauthenticated/half-up gateway) fails.
function Test-IbApiHandshake {
    param(
        [string]$HostName = '127.0.0.1',
        [int]$Port = 4001,
        [int]$TimeoutSec = 6
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connectTask = $client.ConnectAsync($HostName, $Port)
        if (-not $connectTask.Wait($TimeoutSec * 1000)) { return $false }
        if (-not $client.Connected) { return $false }
        $stream = $client.GetStream()
        $stream.ReadTimeout = $TimeoutSec * 1000
        $stream.WriteTimeout = $TimeoutSec * 1000
        # Send API\0 + 4-byte big-endian length + version, exactly like ib_async/ib_hs2.py.
        $ver = [System.Text.Encoding]::ASCII.GetBytes('v100..176')
        $len = $ver.Length
        # BUGFIX 2026-10-17 16:50: the original line built the length bytes with
        # `[byte[]]@( ($len -shr 24) -band 0xFF, ... )` -- PowerShell parses that comma
        # list so that -band's operand becomes an Object[], throws at runtime ("op_BitwiseAnd
        # not found on System.Object[]"), and the catch below silently returned $false.
        # Net effect: the probe had NEVER once returned True (python ib_hs2.py on the same
        # wire answered OK), so every "handshake failed/blocked" in the login logs was this
        # bug, not the gateway. BitConverter+HostToNetworkOrder = plain big-endian int32.
        $lenBytes = [byte[]][System.BitConverter]::GetBytes([System.Net.IPAddress]::HostToNetworkOrder($len))
        $magic = [System.Text.Encoding]::ASCII.GetBytes("API`0")
        $payload = [byte[]]($magic + $lenBytes + $ver)
        $stream.Write($payload, 0, $payload.Length)
        $stream.Flush()
        # Read 4-byte big-endian response length.
        $lenBuf = New-Object byte[] 4
        $read = 0
        while ($read -lt 4) {
            $n = $stream.Read($lenBuf, $read, 4 - $read)
            if ($n -le 0) { return $false }
            $read += $n
        }
        $respLen = ($lenBuf[0] -shl 24) -bor ($lenBuf[1] -shl 16) -bor ($lenBuf[2] -shl 8) -bor $lenBuf[3]
        if ($respLen -le 0 -or $respLen -gt 4096) { return $false }
        # Read the response body.
        $respBuf = New-Object byte[] $respLen
        $read = 0
        while ($read -lt $respLen) {
            $n = $stream.Read($respBuf, $read, $respLen - $read)
            if ($n -le 0) { return $false }
            $read += $n
        }
        # First null-terminated field must be a numeric server version (e.g. "176").
        $fields = [System.Text.Encoding]::ASCII.GetString($respBuf) -split "`0"
        return ($fields[0] -match '^\d+$')
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch {}
    }
}
