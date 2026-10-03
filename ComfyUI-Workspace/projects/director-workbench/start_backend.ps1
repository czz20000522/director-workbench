param([int]$Port = 4100, [string]$HostAddress = '127.0.0.1', [string]$Origins = '')
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $root 'start_private_workbench.ps1') -Port $Port -HostAddress $HostAddress -Origins $Origins -AdminUsers 'user001'
exit $LASTEXITCODE
