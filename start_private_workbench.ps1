param(
    [int]$Port = 4100,
    [string]$HostAddress = '0.0.0.0',
    [string]$AdminUsers = 'user001',
    [string]$Origins = '',
    [string]$AccountsFile = 'D:\Comfy-Desktop\ComfyUI-Workspace\runtime\director-private\accounts-hashed.json',
    [string]$WorkspaceRoot = '',
    [switch]$PrintOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

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

if (-not $Origins) { $Origins = Get-WorkbenchOrigins $Port }
if ($PrintOnly) {
    Write-Output "HostAddress=$HostAddress"
    Write-Output "Port=$Port"
    Write-Output "Origins=$Origins"
    exit 0
}
$env:DIRECTOR_ACCOUNTS_FILE = $AccountsFile
if ($WorkspaceRoot) { $env:DIRECTOR_PRIVATE_ROOT = $WorkspaceRoot }
$env:DIRECTOR_PRIVATE_ORIGINS = $Origins
$env:DIRECTOR_ADMIN_USERS = $AdminUsers
& $python -m uvicorn backend.private_app:app --host $HostAddress --port $Port --workers 1 --app-dir $projectRoot
exit $LASTEXITCODE
