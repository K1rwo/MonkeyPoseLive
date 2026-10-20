[CmdletBinding()]
param([string] $DataDirectory, [switch] $Quiet)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Web-Common.ps1')
if (!$DataDirectory) { $DataDirectory = Get-WebDefaultDataDirectory }
$DataDirectory = Get-WebFullPath $DataDirectory
$launchPath = Join-Path $DataDirectory 'launch.json'
if (!(Test-Path -LiteralPath $launchPath)) { if (!$Quiet) { Write-Host 'No deployment descriptor found.' }; return }
$launch = Get-Content -LiteralPath $launchPath -Raw | ConvertFrom-Json
# Remove external reachability first. Only a matching executable, start time and PID may be stopped.
$tunnel = Get-WebOwnedProcess $launch.tunnel
if ($tunnel) { Stop-Process -Id $tunnel.Id; $tunnel.WaitForExit(5000) | Out-Null }
$server = Get-WebOwnedProcess $launch.server
if ($server) {
    if (!(Test-Path -LiteralPath $launch.report_path)) {
        # No ready descriptor means startup has not completed and no model session can have started.
        Stop-Process -Id $server.Id
        $server.WaitForExit(5000) | Out-Null
    } else {
        $descriptor = Get-Content -LiteralPath $launch.report_path -Raw | ConvertFrom-Json
        if ([int] $descriptor.pid -ne $server.Id) {
            $actualIdentity = Get-WebServerIdentity $launch.server ([int] $descriptor.pid)
            $server = Get-WebOwnedProcess $actualIdentity
            if (!$server) { throw 'The verified server process exited before shutdown.' }
        }
        $shutdown = [Uri] $descriptor.shutdown_url
        if ([int] $descriptor.pid -ne $server.Id -or $shutdown.Scheme -ne 'http' -or
            $shutdown.Host -ne '127.0.0.1' -or $shutdown.Port -ne [int] $launch.port -or
            $shutdown.AbsolutePath -ne '/_local/shutdown' -or $shutdown.UserInfo -or $shutdown.Query -or $shutdown.Fragment) {
            throw 'The private shutdown descriptor does not match this deployment. No server process was stopped.'
        }
        Invoke-RestMethod -Method Post -Uri $shutdown.AbsoluteUri -ContentType 'application/json' -Body '' `
            -Headers @{ 'X-Admin-Token' = $descriptor.shutdown_token } -TimeoutSec 10 -UseBasicParsing | Out-Null
        if (!$server.WaitForExit(20000)) {
            throw 'Shutdown requested, but the server has not exited. The tunnel is closed; inspect the private logs.'
        }
    }
}
$archive = Join-Path $DataDirectory ('launch.stopped.' + (Get-Date -Format 'yyyyMMddTHHmmssfff') + '.json')
Move-Item -LiteralPath $launchPath -Destination $archive
if (!$Quiet) { Write-Host 'This MonkeyPoseLive web deployment has stopped. Local uploads and results were preserved.' }
