param(
    [ValidateSet('all', 'core', 'fast', 'affected', 'auk', 'browser')][string]$Suite = 'all',
    [switch]$Check,
    # Test fixtures write long private-workspace paths and Windows access checks
    # refuse paths past 240 characters. Scratch is project-owned under the
    # checkout's ignored .t directory; shorten fixture names/basetemps instead of
    # moving test output to a drive root, the OS temp dir or the product data root.
    [string]$RuntimeRoot = '',
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
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $projectRoot '.t'
}
$testArguments = @((Join-Path $projectRoot 'tools/run_project_tests.py'), '--suite', $Suite)
if ($Check) { $testArguments += '--check' }
if ($RuntimeRoot) { $testArguments += @('--runtime', $RuntimeRoot) }
if ($Base) { $testArguments += @('--base', $Base) }
if ($WithBrowser) { $testArguments += '--with-browser' }
if ($ImpactConfig) { $testArguments += @('--impact-config', $ImpactConfig) }
& (Join-Path $projectRoot '.venv/Scripts/python.exe') @testArguments
exit $LASTEXITCODE
