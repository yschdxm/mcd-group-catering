# 麦麦团餐统筹官 · 一键环境准备(Windows PowerShell)
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Token "你的MCP Token"
#
# 本项目零第三方依赖,所以本脚本不做 pip 安装,只做三件事:
#   1. 检查 Python 版本是否 >= 3.9
#   2. 自检必需文件与离线快照是否齐全
#   3. 打印/写入环境变量设置方法

[CmdletBinding()]
param(
    [string]$Token = "",
    [switch]$SkipCheck
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

Write-Host ""
Write-Host "🍟 麦麦团餐统筹官 · 环境准备" -ForegroundColor Yellow
Write-Host ("=" * 62)

# ---------------------------------------------------------------- 1. Python
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $versionText = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $versionText) {
                $parts = $versionText.Trim().Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 9) {
                    $python = $candidate
                    break
                }
            }
        } catch { }
    }
}

if (-not $python) {
    Write-Host "  ✘ 未找到 Python 3.9 或更高版本" -ForegroundColor Red
    Write-Host "    请到 https://www.python.org/downloads/ 安装后重试" -ForegroundColor Red
    exit 1
}
Write-Host "  ✔ Python      $(& $python -c 'import sys; print(sys.version.split()[0])')  ($python)"

# ------------------------------------------------------- 2. 必需文件与快照
$required = @(
    "README.md", "CONTEST_DECLARATION.md", "MCP_INTEGRATION.md",
    "mcp-config.example.json", "SKILL.md"
)
$missing = @()
foreach ($file in $required) {
    if (Test-Path (Join-Path $root $file)) {
        Write-Host "  ✔ 必需文件    $file"
    } else {
        Write-Host "  ✘ 必需文件    $file  (缺失)" -ForegroundColor Red
        $missing += $file
    }
}
if ($missing.Count -gt 0) {
    Write-Host "  ✘ 仓库不完整,请重新克隆" -ForegroundColor Red
    exit 1
}

$fixtures = Get-ChildItem (Join-Path $root "fixtures") -File -ErrorAction SilentlyContinue
if ($fixtures) {
    Write-Host "  ✔ 离线快照    $($fixtures.Count) 个文件(可无 Token 演示)"
} else {
    Write-Host "  ⚠ 离线快照目录为空,--offline 模式不可用" -ForegroundColor Yellow
}

# ------------------------------------------------------------- 3. Token 设置
Write-Host ("-" * 62)
if ($Token) {
    $env:MCD_MCP_TOKEN = $Token
    [Environment]::SetEnvironmentVariable("MCD_MCP_TOKEN", $Token, "User")
    Write-Host "  ✔ 已设置 MCD_MCP_TOKEN(当前会话 + 用户级持久化)" -ForegroundColor Green
    Write-Host "    提示:新开的终端才会读到持久化变量" -ForegroundColor DarkGray
} else {
    $existing = [Environment]::GetEnvironmentVariable("MCD_MCP_TOKEN", "User")
    if ($existing) {
        $env:MCD_MCP_TOKEN = $existing
        Write-Host "  ✔ 检测到已存在的 MCD_MCP_TOKEN(掩码:$($existing.Substring(0,4))****$($existing.Substring($existing.Length-4)))"
    } else {
        Write-Host "  ⚠ 未设置 MCD_MCP_TOKEN" -ForegroundColor Yellow
        Write-Host "    申请地址:https://open.mcd.cn/mcp  (登录 → 控制台 → 激活)" -ForegroundColor DarkGray
        Write-Host "    设置命令:  `$env:MCD_MCP_TOKEN = `"你的Token`"" -ForegroundColor DarkGray
        Write-Host "    也可重新运行:  .\install.ps1 -Token `"你的Token`"" -ForegroundColor DarkGray
        Write-Host "    或直接用离线演示:  python scripts/plan.py `"...`" --offline --store 1950564" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------- 4. 自检
Write-Host ("-" * 62)
if (-not $SkipCheck) {
    & $python "scripts/check.py"
}

Write-Host ""
Write-Host "下一步:" -ForegroundColor Yellow
Write-Host "  离线演示(无需 Token):"
Write-Host "    python scripts/plan.py `"14个人 预算480元 不吃辣 两个减脂 12点送到`" --offline --store 1950564"
Write-Host "  真实使用:"
Write-Host "    python scripts/plan.py `"14个人 预算480元 不吃辣 12点送到`" --city 北京"
Write-Host ""
