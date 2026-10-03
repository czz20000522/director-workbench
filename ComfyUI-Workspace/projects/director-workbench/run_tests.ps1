param(
    [ValidateSet('all', 'core', 'auk')][string]$Suite = 'all',
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$testArguments = @((Join-Path $projectRoot 'tools/run_project_tests.py'), '--suite', $Suite)
if ($Check) { $testArguments += '--check' }
& (Join-Path $projectRoot '.venv/Scripts/python.exe') @testArguments
exit $LASTEXITCODE
