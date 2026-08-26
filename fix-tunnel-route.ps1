$ErrorActionPreference = "Stop"
$token = [System.Environment]::GetEnvironmentVariable("CLOUDFLARE_API_TOKEN", "User")
if (-not $token) { throw "CLOUDFLARE_API_TOKEN not found as a User env var." }
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }
$accountId = "b1a31e48f86576dd79c1cb5c349d87a2"
$tunnelId = "9931d150-517b-45fb-b41f-06e5713a2855"
$cfgUri = "https://api.cloudflare.com/client/v4/accounts/$accountId/cfd_tunnel/$tunnelId/configurations"

Write-Host "Fetching current tunnel config..."
$current = Invoke-RestMethod -Uri $cfgUri -Headers $headers -Method Get
if (-not $current.success) { throw "Failed to fetch tunnel config: $($current.errors | ConvertTo-Json -Compress)" }

Write-Host "`nBefore:"
$current.result.config.ingress | ForEach-Object { Write-Host "  $($_.hostname)  ->  $($_.service)" }

$newIngress = $current.result.config.ingress | ForEach-Object {
    if ($_.hostname -eq "quant.carsonng.com") {
        [PSCustomObject]@{ hostname = $_.hostname; service = "http://localhost:18080" }
    } else { $_ }
}

$body = @{ config = @{ ingress = $newIngress } }
if ($current.result.config.warp_routing) { $body.config.warp_routing = $current.result.config.warp_routing }
$bodyJson = $body | ConvertTo-Json -Depth 10

$result = Invoke-RestMethod -Uri $cfgUri -Headers $headers -Method Put -Body $bodyJson
if (-not $result.success) { throw "Update failed: $($result.errors | ConvertTo-Json -Compress)" }
Write-Host "`nApplied. New version: $($result.result.version)"

Write-Host "`nAfter:"
$result.result.config.ingress | ForEach-Object { Write-Host "  $($_.hostname)  ->  $($_.service)" }

Write-Host "`nWaiting for the connector to pick up the new config..."
Start-Sleep -Seconds 6
try {
    $check = Invoke-WebRequest -Uri "https://quant.carsonng.com/" -UseBasicParsing -TimeoutSec 10
    Write-Host "https://quant.carsonng.com/ -> HTTP $($check.StatusCode)"
} catch { Write-Host "check failed: $($_.Exception.Message)" }