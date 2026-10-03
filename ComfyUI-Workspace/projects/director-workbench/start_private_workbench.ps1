param(
    [int]$Port = 4100,
    [string]$HostAddress = '127.0.0.1',
    [Parameter(Mandatory = $true)][string]$AdminUsers,
    [string]$Origins = '',
    [string]$AccountsFile = 'D:\Comfy-Desktop\ComfyUI-Workspace\runtime\director-private\accounts-hashed.json',
    [string]$WorkspaceRoot = 'E:\DirectorWorkspaces'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if ($HostAddress -notin @('127.0.0.1', 'localhost', '::1') -and -not $Origins) {
    throw '局域网启动需要 -Origins，填写允许访问页面的完整地址（例如 http://192.168.1.10:4100）。'
}
if (-not $Origins) { $Origins = "http://127.0.0.1:$Port,http://localhost:$Port" }
$env:DIRECTOR_ACCOUNTS_FILE = $AccountsFile
$env:DIRECTOR_PRIVATE_ROOT = $WorkspaceRoot
$env:DIRECTOR_PRIVATE_ORIGINS = $Origins
$env:DIRECTOR_ADMIN_USERS = $AdminUsers
& $python -m uvicorn backend.private_app:app --host $HostAddress --port $Port --workers 1 --app-dir $projectRoot
exit $LASTEXITCODE
