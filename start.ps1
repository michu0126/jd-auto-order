param(
    [ValidateSet('login', 'prepare', 'check', 'status', 'run', 'dry-run')]
    [string]$Action = 'dry-run',
    [switch]$Now
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Please run setup.ps1 first.'
}
$taskArgs = @('main.py')
if ($Action -eq 'dry-run') { $taskArgs += @('run', '--dry-run') }
else { $taskArgs += $Action }
if ($Now -and $Action -eq 'run') { $taskArgs += '--now' }
& $taskPython @taskArgs
exit $LASTEXITCODE
