param([switch]$NoBrowser, [switch]$WithComfy)

# Idempotent local launcher for the single-user workbench.
# Starts the workbench (4100) only when its port is closed, then waits for
# /api/status before optionally opening the browser. ComfyUI (8188) is on
# demand: the backend starts it from the first generation submission, so it
# stays down while the workbench is merely open. -WithComfy pre-warms it.

$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
$dw = Join-Path $root 'director-workbench'
$logs = Join-Path $root 'director-data\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait(800)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if ($WithComfy -and -not (Test-Port 8188)) {
    $comfy = Join-Path $root 'ComfyUI_windows_portable'
    $py = Join-Path $comfy 'python_embeded\python.exe'
    if (Test-Path $py) {
        Start-Process -FilePath $py `
            -ArgumentList '-s','ComfyUI\main.py','--windows-standalone-build','--offline','--reserve-vram','1.0' `
            -WorkingDirectory $comfy -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logs 'comfyui.out.log') `
            -RedirectStandardError (Join-Path $logs 'comfyui.err.log')
    } else {
        Write-Warning "ComfyUI python not found: $py"
    }
}

if (-not (Test-Port 4100)) {
    $launcher = Join-Path $dw 'start_local.ps1'
    if (-not (Test-Path $launcher)) {
        Write-Error "missing launcher: $launcher"
        exit 1
    }
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Port 4100" `
        -WorkingDirectory $dw -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'workbench.out.log') `
        -RedirectStandardError (Join-Path $logs 'workbench.err.log')
}

$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:4100/api/status' -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    Write-Error 'workbench did not answer on http://127.0.0.1:4100 within 60s'
    exit 1
}

if (-not $NoBrowser) {
    Start-Process 'http://127.0.0.1:4100/'
}
