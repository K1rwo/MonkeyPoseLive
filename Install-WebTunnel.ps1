[CmdletBinding()]
param([string] $ToolsDirectory = (Join-Path $env:LOCALAPPDATA 'MonkeyPoseLive\tools'))
$ErrorActionPreference = 'Stop'
$version = '2026.10.1'
$expected = '4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6'
$url = "https://github.com/cloudflare/cloudflared/releases/download/$version/cloudflared-windows-amd64.exe"
New-Item -ItemType Directory -Path $ToolsDirectory -Force | Out-Null
$destination = Join-Path $ToolsDirectory "cloudflared-$version-windows-amd64.exe"
if (!(Test-Path -LiteralPath $destination)) {
    $temporary = Join-Path $ToolsDirectory ([Guid]::NewGuid().ToString('N') + '.download')
    try {
        Invoke-WebRequest -Uri $url -OutFile $temporary -UseBasicParsing
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw 'SHA256 mismatch. The download was not installed.'
        }
        Move-Item -LiteralPath $temporary -Destination $destination
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}
if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw 'Existing cloudflared does not match the pinned official release. It was not run or replaced.'
}
& $destination --version
Write-Host "Installed verified portable cloudflared: $destination"
Write-Host 'No tunnel has been started.'
