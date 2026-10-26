# Single-user local launcher: unauthenticated backend.app + already-running ComfyUI.
# Source stays pure ASCII on purpose: Windows PowerShell 5.1 decodes a no-BOM UTF-8
# .ps1 as GBK, so the Chinese data-root path is derived from $PSScriptRoot at runtime.
param([int]$Port = 4100, [switch]$PrintOnly)

$root = $PSScriptRoot
$dataRoot = (Resolve-Path (Join-Path $root '..\director-data')).Path
# ComfyUI-Shared/models next to the checkout is the workbench's model inventory root.
$comfyRoot = (Resolve-Path (Join-Path $root '..')).Path
$comfyUrl = if ($env:DIRECTOR_COMFY_URL) { $env:DIRECTOR_COMFY_URL } else { 'http://127.0.0.1:8188' }

if ($PrintOnly) {
    Write-Output "root=$root"
    Write-Output "dataRoot=$dataRoot"
    Write-Output "comfyRoot=$comfyRoot"
    Write-Output "comfyUrl=$comfyUrl"
    Write-Output "port=$Port"
    exit 0
}

$env:DIRECTOR_DATA_ROOT = $dataRoot
$env:DIRECTOR_COMFY_ROOT = $comfyRoot
$env:DIRECTOR_COMFY_URL = $comfyUrl
$env:PYTHONUTF8 = '1'

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { Write-Error "missing venv: $python"; exit 1 }
try {
    Invoke-WebRequest -Uri "$comfyUrl/system_stats" -TimeoutSec 3 -UseBasicParsing | Out-Null
} catch {
    Write-Output "ComfyUI is idle on $comfyUrl - it will start on the first generation."
}

Write-Output "director workbench (single user): http://127.0.0.1:$Port/"
& $python -m uvicorn backend.app:app --host 127.0.0.1 --port $Port --app-dir $root
