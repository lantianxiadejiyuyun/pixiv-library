param([switch]$Intel)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
[string[]]$prefix = @()
if ($Intel) { $prefix = @('--intel') }
& python (Join-Path $projectRoot 'scripts/compose.py') @prefix @args
exit $LASTEXITCODE
