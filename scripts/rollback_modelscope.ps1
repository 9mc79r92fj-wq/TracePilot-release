# TracePilot 魔搭空间一键回滚脚本（安全模式：revert + 重新部署，绝不 force push）
#
# 用途：线上构建失败或发布后健康检查不通过时，立即回到上一个可用版本。
# 前提：设置环境变量 MODELSCOPE_TOKEN（只从环境变量读取，不落盘、不打印）。
#
# 用法（在仓库根目录执行）：
#   $env:MODELSCOPE_TOKEN = "<你的令牌>"
#   .\scripts\rollback_modelscope.ps1
#
# 可选参数：
#   -SpaceRepo <路径>     空间仓库本地克隆目录（默认 %TEMP%\tracepilot-space）
#   -SkipDeploy           只回滚代码，不触发云端重新部署
#   -HealthBase <URL>     回滚后健康检查地址（默认线上短域名）
#
# 回滚后建议再执行：python scripts/verify_public_deploy.py

param(
    [string]$SpaceRepo = (Join-Path $env:TEMP "tracepilot-space"),
    [switch]$SkipDeploy,
    [string]$HealthBase = "https://luis123xi394-tracepilot.ms.show"
)

$ErrorActionPreference = "Stop"
$Owner = "luis123XI394"
$Repo = "TracePilot"

if (-not $env:MODELSCOPE_TOKEN) {
    Write-Error "缺少 MODELSCOPE_TOKEN 环境变量。请先设置：`$env:MODELSCOPE_TOKEN = '<你的令牌>'"
}
$Token = $env:MODELSCOPE_TOKEN

function Invoke-MsApi {
    param([string]$Method, [string]$Path)
    $uri = "https://modelscope.cn" + $Path
    $headers = @{ Authorization = "Bearer $Token"; "User-Agent" = "TracePilot-Rollback/1.0" }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -TimeoutSec 120
}

# 1) 准备空间仓库（不存在则克隆）
if (-not (Test-Path (Join-Path $SpaceRepo ".git"))) {
    $url = "https://oauth2:$Token@www.modelscope.cn/studios/$Owner/$Repo.git"
    Write-Host "[1/4] 克隆空间仓库到 $SpaceRepo"
    git clone $url $SpaceRepo | Out-Host
} else {
    Write-Host "[1/4] 使用已存在的空间仓库：$SpaceRepo"
}
Push-Location $SpaceRepo
try {
    $current = (git rev-parse HEAD).Trim()
    Write-Host "当前线上版本提交：$current"

    # 2) 生成回滚提交（revert 最新一次部署提交）
    Write-Host "[2/4] 生成回滚提交（git revert HEAD）"
    git -c user.name="TracePilot Rollback" -c user.email="rollback@tracepilot.local" revert HEAD --no-edit | Out-Host
    $target = (git rev-parse HEAD).Trim()
    Write-Host "回滚后提交：$target"

    # 3) 推送到空间仓库默认分支（普通 push，不允许 force）
    Write-Host "[3/4] 推送回滚提交（普通 push，不允许 force）"
    git push origin HEAD:master | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "git push 失败" }
} finally {
    Pop-Location
}

if ($SkipDeploy) {
    Write-Host "[4/4] 已跳过部署（-SkipDeploy）。请手动在创空间页面触发重新部署。"
    exit 0
}

# 4) 触发重新部署并等待 Running
Write-Host "[4/4] 触发云端重新部署"
Invoke-MsApi -Method POST -Path "/openapi/v1/studios/$Owner/$Repo/deploy" | Out-Null
$deadline = (Get-Date).AddMinutes(20)
$running = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    try {
        $studio = (Invoke-MsApi -Method GET -Path "/openapi/v1/studios/$Owner/$Repo").data
        $status = [string]$studio.status
        $runtime = [string]$studio.runtime.status
        Write-Host "部署状态：studio=$status runtime=$runtime"
        if ($status -eq "Running" -and $runtime -eq "Running") {
            $running = $true
            break
        }
    } catch {
        Write-Host "轮询异常（继续重试）：$($_.Exception.Message)"
    }
}
if (-not $running) {
    throw "回滚部署超时未进入 Running，请到创空间查看构建日志"
}

# 健康门禁
$health = $null
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 5
    try {
        $health = Invoke-RestMethod -Uri "$HealthBase/api/health" -TimeoutSec 30
        break
    } catch {
        Write-Host "健康检查未就绪（重试 $i/12）：$($_.Exception.Message)"
    }
}
if (-not $health -or $health.status -ne "ok") {
    throw "回滚后健康检查失败：$HealthBase/api/health"
}
Write-Host ""
Write-Host "回滚完成：$HealthBase/api/health -> $($health | ConvertTo-Json -Compress)"
Write-Host "建议再执行：python scripts/verify_public_deploy.py"
