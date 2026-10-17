param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Start',
    [switch]$NoBrowser,
    [int]$WorkbenchPort = 4188,
    [int]$ComfyPort = 8188
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
# This script lives at the project root, which is the Git root and the Python root.
$projectRoot = $root
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$restoreEngine = Join-Path $projectRoot 'tools\restore_comfy_engine.py'
$workbenchScript = Join-Path $projectRoot 'start_private_workbench.ps1'

function Test-TcpPort([string]$HostName, [int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        return $task.Wait(1000) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-TailscaleDnsName {
    try {
        $tailscale = tailscale.exe status --json 2>$null | ConvertFrom-Json
        return ([string]$tailscale.Self.DNSName).TrimEnd('.')
    } catch {
        return ''
    }
}

function Wait-TcpPort([string]$HostName, [int]$Port, [int]$TimeoutSeconds, [string]$Label, [System.Diagnostics.Process]$Process) {
    for ($attempt = 0; $attempt -lt ($TimeoutSeconds * 2); $attempt += 1) {
        if (Test-TcpPort $HostName $Port) { return }
        if ($Process -and $Process.HasExited) {
            throw "$Label 进程已退出，端口 $Port 未就绪。"
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Label 等待超时，端口 $Port 未就绪。"
}

function Get-WorkbenchOrigins([int]$Port) {
    $hosts = [System.Collections.Generic.List[string]]::new()
    foreach ($name in @('127.0.0.1', 'localhost', $env:COMPUTERNAME, [System.Net.Dns]::GetHostName())) {
        if ($name) { $hosts.Add($name.ToLowerInvariant()) }
    }
    $dnsName = Get-TailscaleDnsName
    if ($dnsName) { $hosts.Add($dnsName.ToLowerInvariant()) }
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -and $_.IPAddress -notmatch '^(127\.|169\.254\.|0\.)' } |
        ForEach-Object { $hosts.Add($_.IPAddress) }
    $origins = $hosts | Select-Object -Unique | ForEach-Object { "http://${_}:$Port" }
    # SSH clients use this local browser origin after forwarding 14188 -> 4188.
    $origins += 'http://127.0.0.1:14188', 'http://localhost:14188'
    return ($origins | Select-Object -Unique) -join ','
}

function Get-ListenerProcess([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $connection) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction SilentlyContinue
}

function Wait-PortClosed([int]$Port, [int]$TimeoutSeconds) {
    for ($attempt = 0; $attempt -lt ($TimeoutSeconds * 2); $attempt += 1) {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "端口 $Port 在停止后仍处于监听状态。"
}

function Stop-DirectorServices {
    $workbench = Get-ListenerProcess $WorkbenchPort
    if ($workbench) {
        if ($workbench.CommandLine -notmatch 'director-workbench') {
            throw "端口 $WorkbenchPort 由未知进程占用，未停止：PID $($workbench.ProcessId)。"
        }
        Stop-Process -Id $workbench.ProcessId -Force
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($workbench.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($parent -and $parent.CommandLine -match 'start_private_workbench\.ps1') {
            Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Wait-PortClosed $WorkbenchPort 15
        Write-Host "导演工作台已停止：端口 $WorkbenchPort" -ForegroundColor Green
    } else {
        Write-Host "导演工作台未运行：端口 $WorkbenchPort" -ForegroundColor DarkGray
    }

    $comfy = Get-ListenerProcess $ComfyPort
    if ($comfy) {
        if ($comfy.CommandLine -notmatch 'ComfyUI.*main\.py') {
            throw "端口 $ComfyPort 由未知进程占用，未停止：PID $($comfy.ProcessId)。"
        }
        Stop-Process -Id $comfy.ProcessId -Force
        Wait-PortClosed $ComfyPort 15
        Write-Host "ComfyUI 已停止：端口 $ComfyPort" -ForegroundColor Green
    } else {
        Write-Host "ComfyUI 未运行：端口 $ComfyPort" -ForegroundColor DarkGray
    }
}

function Show-DirectorServiceStatus {
    foreach ($entry in @(@{ Label = 'ComfyUI'; Port = $ComfyPort; Url = "http://127.0.0.1:$ComfyPort/system_stats" }, @{ Label = '导演工作台'; Port = $WorkbenchPort; Url = "http://127.0.0.1:$WorkbenchPort/api/auth/config" })) {
        $process = Get-ListenerProcess $entry.Port
        if (-not $process) {
            Write-Host "$($entry.Label)：未运行（端口 $($entry.Port)）" -ForegroundColor DarkGray
            continue
        }
        try {
            $null = Invoke-RestMethod -Uri $entry.Url -TimeoutSec 5
            Write-Host "$($entry.Label)：运行中且健康（端口 $($entry.Port)，PID $($process.ProcessId)）" -ForegroundColor Green
        } catch {
            Write-Host "$($entry.Label)：端口已监听但健康检查失败（端口 $($entry.Port)，PID $($process.ProcessId)）" -ForegroundColor Yellow
        }
    }
}

if ($Action -eq 'Status') {
    Show-DirectorServiceStatus
    exit 0
}
if ($Action -eq 'Stop') {
    Write-Host '正在停止导演工作台服务……' -ForegroundColor Cyan
    Stop-DirectorServices
    exit 0
}

if (-not (Test-Path -LiteralPath $projectPython)) { throw "工作台 Python 不存在：$projectPython" }
if (-not (Test-Path -LiteralPath $restoreEngine)) { throw "ComfyUI 启动入口不存在：$restoreEngine" }
if (-not (Test-Path -LiteralPath $workbenchScript)) { throw "工作台启动脚本不存在：$workbenchScript" }

Write-Host '导演工作台服务启动器' -ForegroundColor Cyan
Write-Host "根目录：$root" -ForegroundColor DarkGray

if (Test-TcpPort '127.0.0.1' $ComfyPort) {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$ComfyPort/system_stats" -TimeoutSec 5
        Write-Host "ComfyUI 已运行：127.0.0.1:$ComfyPort" -ForegroundColor Green
    } catch {
        throw "端口 $ComfyPort 已被占用，但 ComfyUI 健康检查失败；未启动第二个引擎。"
    }
} else {
    Write-Host '正在启动 ComfyUI，并等待健康检查……' -ForegroundColor Yellow
    & $projectPython $restoreEngine --launch --health-timeout 240
    if ($LASTEXITCODE -ne 0) { throw "ComfyUI 启动失败，退出码 $LASTEXITCODE。请检查 ComfyUI-Workspace/runtime/comfy-recovery/ 中的回执和日志。" }
    Write-Host "ComfyUI 已就绪：127.0.0.1:$ComfyPort" -ForegroundColor Green
}

if (Test-TcpPort '127.0.0.1' $WorkbenchPort) {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkbenchPort/api/auth/config" -TimeoutSec 5
        Write-Host "导演工作台已运行：0.0.0.0:$WorkbenchPort" -ForegroundColor Green
    } catch {
        throw "端口 $WorkbenchPort 已被占用，但导演工作台健康检查失败；未启动第二个服务。"
    }
} else {
    $origins = Get-WorkbenchOrigins $WorkbenchPort
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $workbenchScript,
        '-Port', $WorkbenchPort, '-HostAddress', '0.0.0.0',
        '-AdminUsers', 'user001', '-Origins', $origins
    )
    Write-Host '正在启动导演工作台……' -ForegroundColor Yellow
    $workbenchProcess = Start-Process -FilePath 'pwsh.exe' -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    Wait-TcpPort '127.0.0.1' $WorkbenchPort 30 '导演工作台' $workbenchProcess
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkbenchPort/api/auth/config" -TimeoutSec 5
    Write-Host "导演工作台已就绪：0.0.0.0:$WorkbenchPort" -ForegroundColor Green
}

if (-not $NoBrowser) {
    $url = "http://127.0.0.1:$WorkbenchPort/"
    Start-Process $url
    Write-Host "已打开：$url" -ForegroundColor Cyan
}

Write-Host '服务启动完成。此脚本不启动或修改 Tailscale、SSH。' -ForegroundColor Green
$tailscaleIp = (tailscale.exe ip -4 2>$null | Select-Object -First 1).Trim()
$tailscaleDns = Get-TailscaleDnsName
if ($tailscaleIp) { Write-Host "Tailscale 直连地址：http://${tailscaleIp}:$WorkbenchPort/" -ForegroundColor Cyan }
if ($tailscaleDns) { Write-Host "Tailscale MagicDNS：http://${tailscaleDns}:$WorkbenchPort/" -ForegroundColor Cyan }
if ($tailscaleIp) { Write-Host "SSH 隧道后地址：http://127.0.0.1:14188/（客户端需先转发到 ${tailscaleIp}:127.0.0.1:$WorkbenchPort）" -ForegroundColor DarkCyan }
