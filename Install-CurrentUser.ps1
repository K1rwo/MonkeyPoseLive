param([string]$Destination = (Join-Path $env:LOCALAPPDATA 'Programs\MonkeyPoseLive'))
$ErrorActionPreference = 'Stop'
$source = [System.IO.Path]::GetFullPath($PSScriptRoot)
$target = [System.IO.Path]::GetFullPath($Destination)
if ($target.Equals($source, [System.StringComparison]::OrdinalIgnoreCase) -or $target.StartsWith($source.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Choose an installation destination outside the portable source folder.' }
if (-not (Test-Path -LiteralPath (Join-Path $source 'MonkeyPoseLive.exe'))) { throw 'Run this installer from the extracted portable app folder.' }
if (Test-Path -LiteralPath $target) { throw 'Destination already exists. Choose a new -Destination to preserve the previous version.' }
New-Item -ItemType Directory -Path $target | Out-Null
Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $target -Recurse
$shell = New-Object -ComObject WScript.Shell
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'MonkeyPoseLive.lnk'
$shortcut = $shell.CreateShortcut($menu)
$shortcut.TargetPath = Join-Path $target 'MonkeyPoseLive.exe'
$shortcut.WorkingDirectory = $target
$shortcut.Description = 'MonkeyPoseLive local 2D pose viewer'
$shortcut.Save()
Write-Output ('Installed for this user: ' + $target)
Write-Output 'Model environments, weights and the user configuration are managed separately.'
