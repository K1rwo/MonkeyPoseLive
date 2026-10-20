# Shared Windows PowerShell 5.1 helpers; never print credentials or model configuration.
Set-StrictMode -Version Latest

function Get-WebDefaultDataDirectory {
    Join-Path $env:LOCALAPPDATA 'MonkeyPoseLive\web'
}

function Get-WebFullPath([string] $Value) {
    [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Value))
}

function Protect-WebDirectory([string] $Directory) {
    $target = Get-WebFullPath $Directory
    $repo = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if (($target.TrimEnd('\') + '\').StartsWith($repo, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Web data and credentials must be outside the source/release directory.'
    }
    $marker = Join-Path $target '.monkeypose-web-data'
    if ((Test-Path -LiteralPath $target) -and !(Test-Path -LiteralPath $marker) -and
        @(Get-ChildItem -LiteralPath $target -Force).Count -gt 0) {
        throw 'Choose an empty dedicated web data directory; existing unrelated files were not changed.'
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    $ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowedSids = @($ownerSid, 'S-1-5-18', 'S-1-5-32-544')
    # Modify DACL only. Set-Acl can also attempt SACL writes on Windows PowerShell 5.1,
    # which need SeSecurityPrivilege; this operation must work without administrator rights.
    $grants = $allowedSids | ForEach-Object { '*' + $_ + ':(OI)(CI)F' }
    $aclOutput = & (Join-Path $env:SystemRoot 'System32\icacls.exe') $target '/inheritance:r' '/grant:r' $grants 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Unable to protect the private web data directory.' }
    $acl = Get-Acl -LiteralPath $target
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.IsInherited -or ($rule.AccessControlType -eq 'Allow' -and $sid -notin $allowedSids)) {
            throw 'The web data directory contains unexpected access permissions.'
        }
    }
    if (!(Test-Path -LiteralPath $marker)) { [IO.File]::WriteAllText($marker, 'MonkeyPoseLive web deployment v1') }
    $target
}

function ConvertTo-WebNativeArgument([string] $Value) {
    # CommandLineToArgvW quoting. Start-Process passes this string directly to the EXE.
    $builder = New-Object Text.StringBuilder
    [void] $builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void] $builder.Append(('\' * (2 * $slashes + 1)))
        } else {
            [void] $builder.Append(('\' * $slashes))
        }
        [void] $builder.Append($character)
        $slashes = 0
    }
    [void] $builder.Append(('\' * (2 * $slashes)))
    [void] $builder.Append('"')
    $builder.ToString()
}

function Start-WebOwnedProcess([string] $Executable, [string[]] $Arguments,
                               [string] $WorkingDirectory, [string] $LogPrefix) {
    $argumentLine = ($Arguments | ForEach-Object { ConvertTo-WebNativeArgument $_ }) -join ' '
    $process = Start-Process -FilePath $Executable -ArgumentList $argumentLine -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden -RedirectStandardOutput ($LogPrefix + '.stdout.log') `
        -RedirectStandardError ($LogPrefix + '.stderr.log') -PassThru
    # MainModule/Path can briefly be empty while the Windows child starts.
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $process = Get-Process -Id $process.Id -ErrorAction Stop
        if ($process.Path) { break }
        Start-Sleep -Milliseconds 50
    }
    Get-WebProcessIdentity $process
}

function Get-WebProcessIdentity($Process) {
    [pscustomobject]@{ pid = $Process.Id;
        start_time_ticks = $Process.StartTime.ToUniversalTime().Ticks;
        start_time_utc = $Process.StartTime.ToUniversalTime().ToString('o');
        executable = (Get-WebFullPath $Process.Path) }
}

function Get-WebOwnedProcess($Identity) {
    if ($null -eq $Identity) { return $null }
    $process = Get-Process -Id ([int] $Identity.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    try {
        if ($process.StartTime.ToUniversalTime().Ticks -ne [long] $Identity.start_time_ticks) { return $null }
        if (!(Get-WebFullPath $process.Path).Equals((Get-WebFullPath $Identity.executable), [StringComparison]::OrdinalIgnoreCase)) {
            return $null
        }
    } catch { return $null }
    $process
}

function Get-WebServerIdentity($LauncherIdentity, [int] $ServerPid) {
    $launcher = Get-WebOwnedProcess $LauncherIdentity
    if (!$launcher) { throw 'The launched server process identity changed.' }
    if ($ServerPid -eq $launcher.Id) { return Get-WebProcessIdentity $launcher }
    # Windows venv python.exe may be a launcher for the base interpreter process.
    # Accept the descriptor PID only when its live ancestry reaches our verified launcher.
    $candidate = Get-Process -Id $ServerPid -ErrorAction Stop
    if ($candidate.StartTime.ToUniversalTime().Ticks -lt [long] $LauncherIdentity.start_time_ticks) {
        throw 'The reported server predates this launcher.'
    }
    $currentPid = $ServerPid
    for ($step = 0; $step -lt 8; $step++) {
        $record = Get-CimInstance Win32_Process -Filter "ProcessId = $currentPid"
        if (!$record) { break }
        if ([int] $record.ParentProcessId -eq $launcher.Id) { return Get-WebProcessIdentity $candidate }
        if ([int] $record.ParentProcessId -le 0 -or [int] $record.ParentProcessId -eq $currentPid) { break }
        $currentPid = [int] $record.ParentProcessId
    }
    throw 'The reported server PID is not a child of this deployment.'
}

function Write-WebJson([string] $Path, $Value) {
    $json = $Value | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Read-WebSharedLog([string] $Path) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $reader = New-Object IO.StreamReader($stream)
    try { $reader.ReadToEnd() } finally { $reader.Dispose(); $stream.Dispose() }
}
