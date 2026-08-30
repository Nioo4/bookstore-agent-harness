$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$environmentFile = Join-Path $projectRoot '.env'
if (Test-Path -LiteralPath $environmentFile) {
    throw '.env already exists; preserve it and edit the required settings explicitly.'
}
function New-LocalSecret {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToHexString($bytes).ToLowerInvariant()
}
$values = @{
    MYSQL_ROOT_PASSWORD = New-LocalSecret
    MYSQL_BUSINESS_PASSWORD = New-LocalSecret
    MYSQL_AGENT_PASSWORD = New-LocalSecret
    MYSQL_PUBLISHER_PASSWORD = New-LocalSecret
    GUEST_RATE_LIMIT_HMAC_KEY = New-LocalSecret
    GRAFANA_ADMIN_PASSWORD = New-LocalSecret
}
$lines = Get-Content -LiteralPath (Join-Path $projectRoot '.env.example')
$result = foreach ($line in $lines) {
    $name = ($line -split '=', 2)[0]
    if ($values.ContainsKey($name)) { "$name=$($values[$name])" } else { $line }
}
[IO.File]::WriteAllLines($environmentFile, $result, [Text.UTF8Encoding]::new($false))
Write-Output 'Created .env with fresh local secrets. Configure the model gateway before normal startup; compose.e2e.yaml uses an explicit test fixture model.'
