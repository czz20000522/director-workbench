param([int]$ApiPort = 4100, [int]$DevPort = 4173, [string]$HostAddress = '127.0.0.1', [string]$Origins = '', [switch]$Dev)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($HostAddress -notin @('127.0.0.1', 'localhost', '::1') -and -not $Origins) {
    throw '局域网启动需要 -Origins，填写允许访问页面的完整地址（例如 http://192.168.1.10:4100）。'
}
if ($Dev) {
    $env:DIRECTOR_ADMIN_USERS = 'user001'
    $env:DIRECTOR_PRIVATE_ORIGINS = if ($Origins) { $Origins } else { "http://127.0.0.1:$ApiPort,http://localhost:$ApiPort,http://127.0.0.1:$DevPort,http://localhost:$DevPort" }
    Start-Process -WindowStyle Hidden -FilePath (Join-Path $root '.venv\Scripts\python.exe') -ArgumentList '-m','uvicorn','backend.private_app:app','--host',$HostAddress,'--port',$ApiPort,'--app-dir',$root
    Start-Process -WindowStyle Hidden -FilePath 'npm.cmd' -WorkingDirectory $root -ArgumentList 'run','dev','--','--host',$HostAddress,'--port',$DevPort
    Write-Output "开发页面: http://${HostAddress}:$DevPort/  API: http://${HostAddress}:$ApiPort/"
} else {
    & (Join-Path $root 'start_private_workbench.ps1') -Port $ApiPort -HostAddress $HostAddress -Origins $Origins -AdminUsers 'user001'
    exit $LASTEXITCODE
}
