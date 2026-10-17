param([int]$Port = 4100)

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw '请在管理员 PowerShell 中运行此脚本，以添加仅限专用网络的入站规则。'
}

$displayName = "Director Workbench LAN ($Port)"
$existing = Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue
if (-not $existing) {
    New-NetFirewallRule -DisplayName $displayName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
}

Write-Output "已允许专用网络访问 TCP $Port；ComfyUI 8188 未开放。"
