param([int]$ApiPort = 4100, [int]$DevPort = 4173, [string]$HostAddress = '0.0.0.0', [string]$Origins = '', [switch]$Dev, [switch]$PrintOnly)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
function Get-WorkbenchOrigins([int]$OriginPort) {
    $values = [System.Collections.Generic.List[string]]::new()
    foreach ($hostName in @('127.0.0.1', 'localhost', $env:COMPUTERNAME, [System.Net.Dns]::GetHostName())) {
        if ($hostName) { $values.Add("http://$($hostName.ToLowerInvariant()):$OriginPort") }
    }
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -and $_.IPAddress -notmatch '^(127\.|169\.254\.|0\.)' } |
        ForEach-Object { $values.Add("http://$($_.IPAddress):$OriginPort") }
    return ($values | Select-Object -Unique) -join ','
}
if (-not $Origins) {
    $Origins = Get-WorkbenchOrigins $ApiPort
    if ($Dev) { $Origins = "$Origins,$(Get-WorkbenchOrigins $DevPort)" }
}
if ($PrintOnly) {
    Write-Output "HostAddress=$HostAddress"
    Write-Output "ApiPort=$ApiPort"
    Write-Output "DevPort=$DevPort"
    Write-Output "Origins=$Origins"
    exit 0
}
if ($Dev) {
    $env:DIRECTOR_ADMIN_USERS = 'user001'
    $env:DIRECTOR_PRIVATE_ORIGINS = $Origins
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $root '.venv\Scripts\python.exe') -ArgumentList '-m','uvicorn','backend.private_app:app','--host',$HostAddress,'--port',$ApiPort,'--app-dir',$root
    Start-Process -WindowStyle Hidden -FilePath 'npm.cmd' -WorkingDirectory $root -ArgumentList 'run','dev','--','--host',$HostAddress,'--port',$DevPort
    Write-Output "开发页面: http://${HostAddress}:$DevPort/  API: http://${HostAddress}:$ApiPort/"
} else {
    & (Join-Path $root 'start_private_workbench.ps1') -Port $ApiPort -HostAddress $HostAddress -Origins $Origins -AdminUsers 'user001'
    exit $LASTEXITCODE
}
