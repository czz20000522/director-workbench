param(
    [ValidateSet('all', 'core', 'auk', 'browser')][string]$Suite = 'all',
    [switch]$Check,
    # Test fixtures write long private-workspace paths; Windows access checks in
    # user_context.py refuse paths past 240 characters. Keep the run roots on a
    # short path instead of under the (longer) product data root.
    [string]$RuntimeRoot = 'D:/dw-runtime'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$testArguments = @((Join-Path $projectRoot 'tools/run_project_tests.py'), '--suite', $Suite)
if ($Check) { $testArguments += '--check' }
if ($RuntimeRoot) { $testArguments += @('--runtime', $RuntimeRoot) }
& (Join-Path $projectRoot '.venv/Scripts/python.exe') @testArguments
exit $LASTEXITCODE
