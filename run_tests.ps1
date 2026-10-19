param(
    [ValidateSet('all', 'core', 'fast', 'affected', 'auk', 'browser')][string]$Suite = 'all',
    [switch]$Check,
    # Test fixtures write long private-workspace paths; Windows access checks in
    # user_context.py refuse paths past 240 characters. Keep the run roots on a
    # short path instead of under the (longer) product data root.
    [string]$RuntimeRoot = 'D:/dw-runtime',
    # Affected suite: compare the working tree with this ref instead of the impact
    # config default (usually origin/main). Untracked files are always included.
    [string]$Base = '',
    # Affected suite: also run the browser drivers the selector maps to the change.
    [switch]$WithBrowser,
    # Affected/fast suite: alternate impact config, relative to the project root.
    [string]$ImpactConfig = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$testArguments = @((Join-Path $projectRoot 'tools/run_project_tests.py'), '--suite', $Suite)
if ($Check) { $testArguments += '--check' }
if ($RuntimeRoot) { $testArguments += @('--runtime', $RuntimeRoot) }
if ($Base) { $testArguments += @('--base', $Base) }
if ($WithBrowser) { $testArguments += '--with-browser' }
if ($ImpactConfig) { $testArguments += @('--impact-config', $ImpactConfig) }
& (Join-Path $projectRoot '.venv/Scripts/python.exe') @testArguments
exit $LASTEXITCODE
