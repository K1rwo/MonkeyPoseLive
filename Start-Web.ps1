# Run from this source checkout; model environments remain on the host computer.
[CmdletBinding()]
param(
    [string] $PythonExecutable,
    [string] $ConfigPath,
    [string] $DataDirectory,
    [ValidateRange(1024, 65535)] [int] $Port = 8780,
    [string] $PublicUrl,
    [switch] $QuickTunnel,
    [string] $CloudflaredPath
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Web-Common.ps1')
if ($QuickTunnel -and $PublicUrl) { throw 'Use -QuickTunnel OR -PublicUrl, not both.' }
if (!$DataDirectory) { $DataDirectory = Get-WebDefaultDataDirectory }
$DataDirectory = Protect-WebDirectory $DataDirectory
$launchPath = Join-Path $DataDirectory 'launch.json'
if (Test-Path -LiteralPath $launchPath) {
    $old = Get-Content -LiteralPath $launchPath -Raw | ConvertFrom-Json
    if ((Get-WebOwnedProcess $old.server) -or (Get-WebOwnedProcess $old.tunnel)) {
        throw 'This web deployment is already running. Use Stop-Web.ps1 before starting again.'
    }
    $archive = Join-Path $DataDirectory ('launch.previous.' + (Get-Date -Format 'yyyyMMddTHHmmssfff') + '.json')
    Move-Item -LiteralPath $launchPath -Destination $archive
}
if ($PublicUrl) {
    $origin = [Uri] $PublicUrl
    if ($origin.Scheme -ne 'https' -or !$origin.Host -or $origin.UserInfo -or $origin.Query -or $origin.Fragment -or
        $origin.AbsolutePath -ne '/') { throw '-PublicUrl must be an exact HTTPS origin, such as https://pose.example.org' }
    $PublicUrl = $origin.GetLeftPart([UriPartial]::Authority)
}
if (!$ConfigPath -and $env:MONKEYPOSE_CONFIG) { $ConfigPath = $env:MONKEYPOSE_CONFIG }
if (!$ConfigPath) {
    $defaultConfig = Join-Path $env:LOCALAPPDATA 'MonkeyPoseLive\config.json'
    if (Test-Path -LiteralPath $defaultConfig) { $ConfigPath = $defaultConfig }
}
if ($ConfigPath) { $ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path }
if (!$PythonExecutable -and $ConfigPath) {
    $configuration = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if ($configuration.PSObject.Properties.Name -contains 'base_python') {
        if ($configuration.base_python) { $PythonExecutable = $configuration.base_python }
    }
}
if (!$PythonExecutable) {
    $pythonCommand = Get-Command python.exe -ErrorAction Stop
    $PythonExecutable = $pythonCommand.Source
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
$serverScript = Join-Path $PSScriptRoot 'remote_server.py'
if (!(Test-Path -LiteralPath $serverScript)) { throw 'remote_server.py is missing. Update the source checkout.' }
# Verify the port is free before a tunnel can be created; never proxy an existing desktop service.
$probe = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
try { $probe.Start() } finally { $probe.Stop() }
$runId = Get-Date -Format 'yyyyMMddTHHmmssfff'
$reportPath = Join-Path $DataDirectory ('server.' + $runId + '.json')
$launch = [ordered]@{ schema_version = 1; created_at = [DateTime]::UtcNow.ToString('o');
    server = $null; tunnel = $null; port = $Port; public_url = $PublicUrl; report_path = $reportPath }
try {
    if ($QuickTunnel) {
        if (!$CloudflaredPath) { $CloudflaredPath = Join-Path $env:LOCALAPPDATA 'MonkeyPoseLive\tools\cloudflared-2026.10.1-windows-amd64.exe' }
        $CloudflaredPath = (Resolve-Path -LiteralPath $CloudflaredPath).Path
        $emptyConfig = Join-Path $DataDirectory 'quick-tunnel.yml'
        [IO.File]::WriteAllText($emptyConfig, "# Isolated Quick Tunnel configuration.`n", (New-Object Text.UTF8Encoding($false)))
        $tunnelLog = Join-Path $DataDirectory ('tunnel.' + $runId)
        $launch.tunnel = Start-WebOwnedProcess $CloudflaredPath @('tunnel', '--config', $emptyConfig,
            '--no-autoupdate', '--url', "http://127.0.0.1:$Port", '--metrics', '127.0.0.1:0') $DataDirectory $tunnelLog
        Write-WebJson $launchPath $launch
        # The origin port remains closed until an exact public hostname has been registered.
        $deadline = [DateTime]::UtcNow.AddSeconds(55)
        while ([DateTime]::UtcNow -lt $deadline) {
            if (!(Get-WebOwnedProcess $launch.tunnel)) { throw 'cloudflared exited. See the private tunnel logs.' }
            $logText = ''
            foreach ($suffix in @('.stderr.log', '.stdout.log')) {
                if (Test-Path -LiteralPath ($tunnelLog + $suffix)) { $logText += Read-WebSharedLog ($tunnelLog + $suffix) }
            }
            if ($logText -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $PublicUrl = $Matches[0]; break }
            Start-Sleep -Milliseconds 250
        }
        if (!$PublicUrl) { throw 'Quick Tunnel did not provide a URL within 55 seconds. No web service was exposed.' }
        $launch.public_url = $PublicUrl
    }
    $serverArguments = @($serverScript, '--host', '127.0.0.1', '--port', [string] $Port,
        '--data-dir', $DataDirectory, '--report', $reportPath)
    if ($PublicUrl) { $serverArguments += @('--public-url', $PublicUrl) }
    if ($ConfigPath) { $serverArguments += @('--config', $ConfigPath) }
    $launch.server = Start-WebOwnedProcess $PythonExecutable $serverArguments $PSScriptRoot (Join-Path $DataDirectory ('server.' + $runId))
    Write-WebJson $launchPath $launch
    $deadline = [DateTime]::UtcNow.AddSeconds(40)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if (!(Get-WebOwnedProcess $launch.server)) { throw 'The web server exited. See the private server logs.' }
        if (Test-Path -LiteralPath $reportPath) {
            try {
                $descriptor = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
                $serverIdentity = Get-WebServerIdentity $launch.server ([int] $descriptor.pid)
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 -UseBasicParsing
                if ($health.app -eq 'MonkeyPoseLiveWeb') {
                    $launch.server = $serverIdentity
                    Write-WebJson $launchPath $launch
                    $ready = $true
                    break
                }
            } catch { }
        }
        Start-Sleep -Milliseconds 200
    }
    if (!$ready) { throw 'The server was not ready within 40 seconds.' }
    Write-Host 'MonkeyPoseLive web service is running.'
    if ($PublicUrl) { Write-Host "Access URL: $PublicUrl" } else { Write-Host "Local preview: http://127.0.0.1:$Port" }
    Write-Host "Private login instructions: $(Join-Path $DataDirectory 'access.txt')"
    Write-Host "Stop with: .\Stop-Web.ps1 -DataDirectory `"$DataDirectory`""
} catch {
    # Reuse the same verified lifecycle routine on partially completed starts.
    Write-WebJson $launchPath $launch
    & (Join-Path $PSScriptRoot 'Stop-Web.ps1') -DataDirectory $DataDirectory -Quiet
    throw
}
