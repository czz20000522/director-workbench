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

# Optional local deployment profile. Kept out of the public repository because it names
# the operator's own tunnel target and public origins. When present and enabled, the
# workbench binds loopback and the public entry is reached through a reverse SSH tunnel.
$deploymentProfile = Join-Path $projectRoot '.config\director-workbench\public-mode.psd1'
$public = $null
if (Test-Path -LiteralPath $deploymentProfile) {
    try {
        $public = Import-PowerShellDataFile -LiteralPath $deploymentProfile
    } catch {
        throw "公网部署配置无效：$deploymentProfile（$($_.Exception.Message)）"
    }
}
$publicEnabled = [bool]($public -and $public.Enabled)
if ($publicEnabled) {
    if ($public.WorkbenchPort) { $WorkbenchPort = [int]$public.WorkbenchPort }
    if ($public.ComfyPort) { $ComfyPort = [int]$public.ComfyPort }
}
$workbenchHost = if ($publicEnabled -and $public.WorkbenchHost) { [string]$public.WorkbenchHost } else { '0.0.0.0' }
$workbenchOrigins = if ($publicEnabled -and $public.Origins) { [string]$public.Origins } else { '' }
$secureCookie = [bool]($publicEnabled -and $public.SecureCookie)
$mcpAllowedHosts = if ($publicEnabled -and $public.McpAllowedHosts) { [string]$public.McpAllowedHosts } else { '' }
$mcpAllowedOrigins = if ($publicEnabled -and $public.McpAllowedOrigins) { [string]$public.McpAllowedOrigins } else { '' }
$tunnelTarget = if ($publicEnabled) { [string]$public.TunnelTarget } else { '' }
$tunnelRemotePort = if ($publicEnabled -and $public.TunnelRemotePort) { [int]$public.TunnelRemotePort } else { $WorkbenchPort }
$tunnelLocalPort = if ($publicEnabled -and $public.TunnelLocalPort) { [int]$public.TunnelLocalPort } else { $WorkbenchPort }
$runtimeDir = Join-Path $projectRoot '.config\director-workbench\runtime'
$tunnelPidFile = Join-Path $runtimeDir 'tunnel.pid'
$tunnelLog = Join-Path $runtimeDir 'tunnel.log'

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

function Get-ManagedTunnelProcess {
    if (-not $publicEnabled -or -not (Test-Path -LiteralPath $tunnelPidFile)) { return $null }
    $raw = Get-Content -LiteralPath $tunnelPidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $tunnelPid = 0
    if (-not [int]::TryParse([string]$raw, [ref]$tunnelPid)) {
        Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
        return $null
    }
    $process = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
    if ($process) { return $process }
    Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
    return $null
}

function Stop-ManagedTunnel {
    if (-not $publicEnabled) { return }
    $process = Get-ManagedTunnelProcess
    if ($process) {
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
        Write-Host "反向隧道已停止：PID $($process.Id)" -ForegroundColor Green
    } else {
        Write-Host '反向隧道未运行' -ForegroundColor DarkGray
    }
    $forward = [regex]::Escape("-R 127.0.0.1:${tunnelRemotePort}:127.0.0.1:${tunnelLocalPort}")
    $forwardText = "-R 127.0.0.1:${tunnelRemotePort}:127.0.0.1:${tunnelLocalPort}"
    for ($attempt = 0; $attempt -lt 3; $attempt += 1) {
        Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match $forward } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        # A launcher loop keeps restarting ssh; stop the loop process too. Some older
        # launchers pass the loop through -EncodedCommand, so decode before matching.
        Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID } |
            ForEach-Object {
                $commandLine = [string]$_.CommandLine
                $matchesForward = $commandLine -match $forward
                if (-not $matchesForward -and $commandLine -match '-EncodedCommand\s+(\S+)') {
                    try {
                        $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
                        $matchesForward = $decoded.Contains($forwardText)
                    } catch { $matchesForward = $false }
                }
                if ($matchesForward) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            }
        Start-Sleep -Seconds 1
        $remaining = Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match $forward }
        if (-not $remaining) { break }
    }
}

function Start-ManagedTunnel {
    if (-not $publicEnabled) { return }
    if (-not $tunnelTarget) { throw '公网部署配置缺少 TunnelTarget，无法启动反向隧道。' }
    $existing = Get-ManagedTunnelProcess
    if ($existing) {
        Write-Host "反向隧道已运行：PID $($existing.Id) -> $tunnelTarget" -ForegroundColor Green
        return
    }
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $loop = "while (`$true) { ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:${tunnelRemotePort}:127.0.0.1:${tunnelLocalPort} ${tunnelTarget} *>> '$tunnelLog'; Start-Sleep -Seconds 5 }"
    $process = Start-Process -FilePath 'pwsh.exe' -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-Command', $loop -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $tunnelPidFile -Value $process.Id -Encoding utf8
    Write-Host "反向隧道已启动：PID $($process.Id) -> $tunnelTarget（远端 127.0.0.1:${tunnelRemotePort}）" -ForegroundColor Green
}

function Invoke-ComfyControl([string]$Path, $Body) {
    if (-not (Test-TcpPort '127.0.0.1' $ComfyPort)) { return }
    try {
        if ($null -ne $Body) {
            $payload = $Body | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri "http://127.0.0.1:$ComfyPort$Path" -Method Post -ContentType 'application/json' -Body $payload -TimeoutSec 3 | Out-Null
        } else {
            Invoke-RestMethod -Uri "http://127.0.0.1:$ComfyPort$Path" -Method Post -TimeoutSec 3 | Out-Null
        }
    } catch { }
}

function Release-ComfyResources {
    if (-not (Test-TcpPort '127.0.0.1' $ComfyPort)) { return }
    Write-Host '正在取消当前生成并请求 ComfyUI 卸载模型、释放显存……' -ForegroundColor Yellow
    Invoke-ComfyControl '/interrupt' $null
    Invoke-ComfyControl '/free' @{ unload_models = $true; free_memory = $true }
    Start-Sleep -Seconds 2
}

function Stop-DirectorServices {
    if ($publicEnabled) { Stop-ManagedTunnel }
    Release-ComfyResources

    $workbench = Get-ListenerProcess $WorkbenchPort
    if ($workbench) {
        if ($workbench.CommandLine -notmatch 'director-workbench') {
            throw "端口 $WorkbenchPort 由未知进程占用，未停止：PID $($workbench.ProcessId)。"
        }
        & taskkill.exe /PID $($workbench.ProcessId) /T /F 2>$null | Out-Null
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
        & taskkill.exe /PID $($comfy.ProcessId) /T /F 2>$null | Out-Null
        Wait-PortClosed $ComfyPort 15
        Write-Host "ComfyUI 已停止：端口 $ComfyPort" -ForegroundColor Green
    } else {
        Write-Host "ComfyUI 未运行：端口 $ComfyPort" -ForegroundColor DarkGray
    }

    # Defensive cleanup: renderer/builder children can outlive a force-killed parent on Windows.
    $leftover = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'director-workbench|h3_render\.py|build_assembly_workflow\.py|build_video_finish_workflow\.py' }
    foreach ($process in $leftover) {
        & taskkill.exe /PID $($process.ProcessId) /T /F 2>$null | Out-Null
    }
    if ($leftover) { Write-Host '已清理残留的渲染/构建子进程。' -ForegroundColor Green }

    Start-Sleep -Seconds 1
    $gpuUsed = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $gpuUsed) {
        Write-Host "当前显存占用：$gpuUsed MiB（工作台与引擎进程已结束，驱动会回收其余占用）" -ForegroundColor Green
    }
    Write-Host '工作台内存与显存资源已释放。' -ForegroundColor Green
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
    if ($publicEnabled) {
        $tunnel = Get-ManagedTunnelProcess
        if ($tunnel) {
            Write-Host "反向隧道：运行中（PID $($tunnel.Id) -> $tunnelTarget，远端 127.0.0.1:${tunnelRemotePort}）" -ForegroundColor Green
        } else {
            Write-Host '反向隧道：未运行（公网入口将不可用）' -ForegroundColor Red
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
    if ($LASTEXITCODE -ne 0) { throw "ComfyUI 启动失败，退出码 $LASTEXITCODE。请检查工作台数据根 runtime/comfy-recovery/ 中的回执和日志。" }
    Write-Host "ComfyUI 已就绪：127.0.0.1:$ComfyPort" -ForegroundColor Green
}

if (Test-TcpPort '127.0.0.1' $WorkbenchPort) {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkbenchPort/api/auth/config" -TimeoutSec 5
        Write-Host "导演工作台已运行：${workbenchHost}:$WorkbenchPort" -ForegroundColor Green
    } catch {
        throw "端口 $WorkbenchPort 已被占用，但导演工作台健康检查失败；未启动第二个服务。"
    }
} else {
    $origins = if ($workbenchOrigins) { $workbenchOrigins } else { Get-WorkbenchOrigins $WorkbenchPort }
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $workbenchScript,
        '-Port', $WorkbenchPort, '-HostAddress', $workbenchHost,
        '-AdminUsers', 'user001', '-Origins', $origins
    )
    Write-Host '正在启动导演工作台……' -ForegroundColor Yellow
    $previousCookieSecure = $env:DIRECTOR_COOKIE_SECURE
    $previousMcpHosts = $env:DIRECTOR_MCP_ALLOWED_HOSTS
    $previousMcpOrigins = $env:DIRECTOR_MCP_ALLOWED_ORIGINS
    try {
        if ($secureCookie) { $env:DIRECTOR_COOKIE_SECURE = '1' } else { Remove-Item Env:DIRECTOR_COOKIE_SECURE -ErrorAction SilentlyContinue }
        if ($mcpAllowedHosts) { $env:DIRECTOR_MCP_ALLOWED_HOSTS = $mcpAllowedHosts } else { Remove-Item Env:DIRECTOR_MCP_ALLOWED_HOSTS -ErrorAction SilentlyContinue }
        if ($mcpAllowedOrigins) { $env:DIRECTOR_MCP_ALLOWED_ORIGINS = $mcpAllowedOrigins } else { Remove-Item Env:DIRECTOR_MCP_ALLOWED_ORIGINS -ErrorAction SilentlyContinue }
        $workbenchProcess = Start-Process -FilePath 'pwsh.exe' -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
    } finally {
        if ($null -ne $previousCookieSecure) { $env:DIRECTOR_COOKIE_SECURE = $previousCookieSecure } else { Remove-Item Env:DIRECTOR_COOKIE_SECURE -ErrorAction SilentlyContinue }
        if ($null -ne $previousMcpHosts) { $env:DIRECTOR_MCP_ALLOWED_HOSTS = $previousMcpHosts } else { Remove-Item Env:DIRECTOR_MCP_ALLOWED_HOSTS -ErrorAction SilentlyContinue }
        if ($null -ne $previousMcpOrigins) { $env:DIRECTOR_MCP_ALLOWED_ORIGINS = $previousMcpOrigins } else { Remove-Item Env:DIRECTOR_MCP_ALLOWED_ORIGINS -ErrorAction SilentlyContinue }
    }
    Wait-TcpPort '127.0.0.1' $WorkbenchPort 30 '导演工作台' $workbenchProcess
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkbenchPort/api/auth/config" -TimeoutSec 5
    Write-Host "导演工作台已就绪：${workbenchHost}:$WorkbenchPort" -ForegroundColor Green
}

if ($publicEnabled) {
    Start-ManagedTunnel
}

if (-not $NoBrowser) {
    $url = "http://127.0.0.1:$WorkbenchPort/"
    Start-Process $url
    Write-Host "已打开：$url" -ForegroundColor Cyan
}

Write-Host '服务启动完成。此脚本不启动或修改 Tailscale、SSH 配置。' -ForegroundColor Green
if ($publicEnabled) {
    Write-Host "公网入口：由服务器 Nginx 反向代理到本机隧道（${tunnelTarget} -> 127.0.0.1:${tunnelRemotePort}）" -ForegroundColor Cyan
    Write-Host "本地管理地址：http://127.0.0.1:$WorkbenchPort/" -ForegroundColor Cyan
    Write-Host '反向隧道日志：.config\director-workbench\runtime\tunnel.log' -ForegroundColor DarkGray
} else {
    $tailscaleIp = (tailscale.exe ip -4 2>$null | Select-Object -First 1).Trim()
    $tailscaleDns = Get-TailscaleDnsName
    if ($tailscaleIp) { Write-Host "Tailscale 直连地址：http://${tailscaleIp}:$WorkbenchPort/" -ForegroundColor Cyan }
    if ($tailscaleDns) { Write-Host "Tailscale MagicDNS：http://${tailscaleDns}:$WorkbenchPort/" -ForegroundColor Cyan }
    if ($tailscaleIp) { Write-Host "SSH 隧道后地址：http://127.0.0.1:14188/（客户端需先转发到 ${tailscaleIp}:127.0.0.1:$WorkbenchPort）" -ForegroundColor DarkCyan }
}
