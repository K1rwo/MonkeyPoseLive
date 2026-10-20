# Run on Windows with powershell.exe or pwsh.exe; never starts a tunnel or model.
$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'Web-Common.ps1')
function Assert-Web([bool] $Condition, [string] $Message) { if (!$Condition) { throw $Message } }
$current = Get-Process -Id $PID
$identity = Get-WebProcessIdentity $current
# ConvertFrom-Json may coerce ISO dates in PowerShell 7; identity must retain subsecond precision.
$roundTrip = $identity | ConvertTo-Json | ConvertFrom-Json
Assert-Web ([bool](Get-WebOwnedProcess $roundTrip)) 'Serialized process identity lost precision.'
$wrongTime = $identity | ConvertTo-Json | ConvertFrom-Json
$wrongTime.start_time_ticks = [long] $wrongTime.start_time_ticks + 1
Assert-Web ($null -eq (Get-WebOwnedProcess $wrongTime)) 'A mismatched start time must never match.'
$wrongPath = $identity | ConvertTo-Json | ConvertFrom-Json
$wrongPath.executable = 'C:\does-not-exist\python.exe'
Assert-Web ($null -eq (Get-WebOwnedProcess $wrongPath)) 'A mismatched executable must never match.'
Assert-Web ((ConvertTo-WebNativeArgument 'C:\a b\') -eq '"C:\a b\\"') 'Trailing slash quoting failed.'
Assert-Web ((ConvertTo-WebNativeArgument 'a"b') -eq '"a\"b"') 'Embedded quote escaping failed.'
$qa = Join-Path $env:LOCALAPPDATA ('MonkeyPoseLive\launcher-test-' + [Guid]::NewGuid().ToString('N'))
try {
    $protected = Protect-WebDirectory $qa
    Assert-Web (Test-Path -LiteralPath (Join-Path $protected '.monkeypose-web-data')) 'Data ownership marker missing.'
    $acl = Get-Acl -LiteralPath $qa
    Assert-Web $acl.AreAccessRulesProtected 'Private directory inherited broad ACLs.'
    Assert-Web (@($acl.Access | Where-Object IsInherited).Count -eq 0) 'Private directory has inherited ACLs.'
    Assert-Web ((Protect-WebDirectory $qa) -eq $protected) 'An owned directory should reopen.'
    $sharedLog = Join-Path $qa 'shared-log-test.log'
    $writer = [IO.File]::Open($sharedLog, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes('tunnel log while running')
        $writer.Write($bytes, 0, $bytes.Length)
        $writer.Flush()
        Assert-Web ((Read-WebSharedLog $sharedLog) -eq 'tunnel log while running') 'A running tunnel log must be readable.'
    } finally { $writer.Dispose() }
    Write-Host 'Web launcher checks passed: identity round trip, stale identity, argument quoting, private ACL, owned restart and live log reads.'
} finally {
    # This test only removes the exact two files it creates, then the verified empty test directory.
    $testRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'MonkeyPoseLive')).TrimEnd('\') + '\'
    $resolvedQa = [IO.Path]::GetFullPath($qa)
    if (!$resolvedQa.StartsWith($testRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected test cleanup target.' }
    $marker = Join-Path $resolvedQa '.monkeypose-web-data'
    $sharedLog = Join-Path $resolvedQa 'shared-log-test.log'
    if (Test-Path -LiteralPath $sharedLog) { Remove-Item -LiteralPath $sharedLog }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
    if ((Test-Path -LiteralPath $resolvedQa) -and @(Get-ChildItem -LiteralPath $resolvedQa -Force).Count -eq 0) {
        Remove-Item -LiteralPath $resolvedQa
    }
}
