param([int]$Port = 4100, [string]$HostAddress = '0.0.0.0', [string]$Origins = '', [switch]$PrintOnly)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $root 'start_private_workbench.ps1') -Port $Port -HostAddress $HostAddress -Origins $Origins -AdminUsers 'user001' -PrintOnly:$PrintOnly
exit $LASTEXITCODE
