<#
.SYNOPSIS
  aicreat 本机首次初始化并启动（Windows）：环境变量文件、MySQL / Redis（Docker）、后端 venv 与依赖、迁移与 seed、
  前端依赖与共享包，最后调用 scripts/dev-restart.ps1 启动 API、app.worker、app.monitor_worker 与管理后台。

.DESCRIPTION
  把 docs/06-getting-started.md 第一～四节串成一条命令；每一步都幂等，可重复执行：
  1. 检查 Python 3.11+、Node.js 18+、pnpm、Docker（-SkipDocker 时不检查 Docker）；
  2. 根 .env、server/.env 不存在时从 .env.example 复制；已存在则不改动（不会覆盖已填写的 ZHIQI_API_KEY）；
  3. docker compose up -d mysql redis 并等待两者 healthy；-SkipDocker 时改用本机已装的 MySQL / Redis
     （按 server/.env 的 DATABASE_URL / REDIS_URL 连接，库与账号须已按 docs/06 第一节建好）；
  4. server/.venv 不存在时用 Python 3.11+ 创建，再 pip install -e ".[dev]"；
  5. alembic upgrade head、python seeds/seed.py；
  6. 仓库根 pnpm install、pnpm build:shared；
  7. 调用 scripts/dev-restart.ps1 启动四个进程并做健康检查（-NoStart 时跳过）。
  ZHIQI_API_KEY 由使用者自己填入 server/.env：为空时以 Mock 模式运行，填好后执行 pnpm dev:restart 生效（docs/06 第六节）。

.PARAMETER SkipDocker
  不用 Docker 启动 MySQL / Redis，改用本机已装的服务。

.PARAMETER NoStart
  只做初始化，不调用 dev-restart.ps1 启动进程。

.EXAMPLE
  pnpm dev:setup
  powershell -ExecutionPolicy Bypass -File scripts/dev-setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts/dev-setup.ps1 -SkipDocker
#>
[CmdletBinding()]
param(
    [switch]$SkipDocker,
    [switch]$NoStart
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $Root 'server'
$VenvDir = Join-Path $ServerDir '.venv'
$VenvPython = Join-Path $ServerDir '.venv\Scripts\python.exe'
$RootEnv = Join-Path $Root '.env'
$ServerEnv = Join-Path $ServerDir '.env'
$RestartScript = Join-Path $PSScriptRoot 'dev-restart.ps1'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$MinPython = [version]'3.11'
$MinNodeMajor = 18

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-Warn([string]$Message) { Write-Host "    [警告] $Message" -ForegroundColor Yellow }

# ------------------------------------------------------------------ 原生命令

function Invoke-Native([string]$What, [string]$WorkingDirectory, [string]$FilePath, [string[]]$Arguments) {
    # 不重定向 stderr：Windows PowerShell 5.1 在 ErrorActionPreference=Stop 下会把重定向后的 stderr 当作终止错误
    # 同理把本函数内的 ErrorActionPreference 降为 Continue：经 pnpm 等管道启动时 stderr 也可能被包装成错误记录，成败只看退出码
    $ErrorActionPreference = 'Continue'
    Write-Info "$What：$FilePath $($Arguments -join ' ')"
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $FilePath @Arguments
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { throw "$What 失败（退出码 $code），请查看上方输出。" }
}

function Get-NativeOutput([string]$FilePath, [string[]]$Arguments) {
    # 探测类命令：失败（命令不存在、非零退出、写 stderr）一律返回 $null，不中断脚本
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (@($output) -join "`n").Trim()
    } catch {
        return $null
    }
}

# ------------------------------------------------------------------ 前置检查

function Find-Python {
    # py 启动器优先（可同时装有多个版本），再试 PATH 上的 python；Microsoft Store 的 python 占位程序输出不是版本号，自动跳过。
    # -c 代码里不写引号：Windows PowerShell 5.1 向原生命令传参时不转义内嵌双引号
    $candidates = @(
        @{ File = 'py'; Args = @('-3.11') },
        @{ File = 'py'; Args = @('-3.12') },
        @{ File = 'py'; Args = @('-3.13') },
        @{ File = 'py'; Args = @('-3') },
        @{ File = 'python'; Args = @() },
        @{ File = 'python3'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.File -ErrorAction SilentlyContinue)) { continue }
        $text = Get-NativeOutput $candidate.File ($candidate.Args + @('-c', 'import sys; print(*sys.version_info[:2], sep=chr(46))'))
        if ($null -eq $text -or $text -notmatch '^\d+\.\d+$') { continue }
        if ([version]$text -ge $MinPython) {
            return [pscustomobject]@{ File = $candidate.File; Args = $candidate.Args; Version = $text }
        }
    }
    return $null
}

function Assert-Prerequisites {
    if ($env:OS -ne 'Windows_NT') {
        throw 'dev-setup.ps1 仅用于 Windows；macOS / Linux 请按 docs/06-getting-started.md 第一～四节逐步执行。'
    }

    if (Test-Path -LiteralPath $VenvPython) {
        Write-Info "Python   使用已有虚拟环境 server\.venv"
        $script:Python = $null
    } else {
        $script:Python = Find-Python
        if ($null -eq $script:Python) {
            throw "未找到 Python $MinPython+：请从 https://www.python.org/downloads/ 安装（勾选 Add python.exe to PATH）后重新打开终端。"
        }
        Write-Info "Python   $($script:Python.Version)（$($script:Python.File) $($script:Python.Args -join ' ')）"
    }

    $nodeVersion = Get-NativeOutput 'node' @('--version')
    if ($null -eq $nodeVersion -or $nodeVersion -notmatch '^v(\d+)\.') {
        throw "未找到 Node.js：请安装 Node.js 20 LTS（最低 $MinNodeMajor）后重新打开终端。"
    }
    if ([int]$Matches[1] -lt $MinNodeMajor) {
        throw "Node.js 版本过低（$nodeVersion）：最低 $MinNodeMajor，推荐 20 LTS。"
    }
    Write-Info "Node.js  $nodeVersion"

    $pnpmVersion = Get-NativeOutput 'pnpm' @('--version')
    if ($null -eq $pnpmVersion) {
        throw '未找到 pnpm：执行 npm i -g pnpm@9（或 corepack enable）后重新打开终端。'
    }
    Write-Info "pnpm     $pnpmVersion"

    if ($SkipDocker) {
        Write-Info 'Docker   已跳过（-SkipDocker：使用本机 MySQL / Redis）'
        return
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw '未找到 docker：请安装并启动 Docker Desktop；或改用本机已装的 MySQL 8 / Redis 7，并以 -SkipDocker 运行本脚本。'
    }
    if ($null -eq (Get-NativeOutput 'docker' @('info', '--format', '{{.ServerVersion}}'))) {
        throw 'Docker 未运行：请先启动 Docker Desktop，等它显示 Engine running 后重试。'
    }
    $composeVersion = Get-NativeOutput 'docker' @('compose', 'version', '--short')
    if ($null -eq $composeVersion) {
        throw '当前 Docker 不支持 docker compose（v2）子命令：请升级 Docker Desktop。'
    }
    Write-Info "Docker   compose $composeVersion"
}

# ------------------------------------------------------------------ 环境变量文件

function Copy-EnvTemplate([string]$Target, [string]$Template, [string]$Label) {
    if (Test-Path -LiteralPath $Target) {
        Write-Info "$Label 已存在，保持不变"
        return
    }
    Copy-Item -LiteralPath $Template -Destination $Target
    Write-Info "已从 $(Split-Path -Leaf $Template) 复制 $Label"
}

function Get-EnvValue([string]$Path, [string]$Key) {
    # 只读单个键：去掉行尾 # 注释与成对引号；键不存在返回 $null
    foreach ($line in [System.IO.File]::ReadAllLines($Path, $Utf8NoBom)) {
        $m = [regex]::Match($line, "^\s*$([regex]::Escape($Key))\s*=(?<raw>.*)$")
        if (-not $m.Success) { continue }
        $value = [regex]::Replace($m.Groups['raw'].Value, '\s+#.*$', '').Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        return $value
    }
    return $null
}

# ------------------------------------------------------------------ MySQL / Redis

function Wait-ComposeHealthy([string[]]$Services, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $pending = @($Services)
    Push-Location -LiteralPath $Root
    try {
        while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
            $still = @()
            foreach ($service in $pending) {
                $id = Get-NativeOutput 'docker' @('compose', 'ps', '-q', $service)
                $status = $null
                if ($id) { $status = Get-NativeOutput 'docker' @('inspect', '-f', '{{.State.Health.Status}}', ($id -split "`n")[0]) }
                if ($status -eq 'healthy') {
                    Write-Info "$service healthy"
                } else {
                    $still += $service
                }
            }
            $pending = $still
            if ($pending.Count -gt 0) { Start-Sleep -Seconds 3 }
        }
    } finally {
        Pop-Location
    }
    if ($pending.Count -gt 0) {
        throw "等待 $($pending -join ' / ') healthy 超时（$TimeoutSeconds 秒）：执行 docker compose ps 与 docker compose logs mysql 查看原因（常见：根 .env 缺 MYSQL_*、3306 / 6379 被本机服务占用）。"
    }
}

# ------------------------------------------------------------------ 主流程

Write-Step '检查前置工具'
Assert-Prerequisites

Write-Step '环境变量文件'
Copy-EnvTemplate $RootEnv (Join-Path $Root '.env.example') '根 .env'
Copy-EnvTemplate $ServerEnv (Join-Path $ServerDir '.env.example') 'server\.env'

if ($SkipDocker) {
    Write-Step 'MySQL / Redis：使用本机服务（-SkipDocker）'
    Write-Info "DATABASE_URL / REDIS_URL 取自 server\.env；库与账号须已按 docs/06 第一节建好"
} else {
    Write-Step 'MySQL / Redis：docker compose up -d mysql redis'
    try {
        Invoke-Native '启动 mysql / redis 容器' $Root 'docker' @('compose', 'up', '-d', 'mysql', 'redis')
    } catch {
        throw "$($_.Exception.Message) 若 3306 / 6379 已被本机 MySQL / Redis 占用：停止这些服务后重试，或直接使用它们并以 -SkipDocker 运行本脚本。"
    }
    Write-Info '等待 healthy（首次初始化 MySQL 约 30~100 秒）'
    Wait-ComposeHealthy @('mysql', 'redis') 240
}

Write-Step '后端依赖：server\.venv'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    if (Test-Path -LiteralPath $VenvDir) {
        throw "server\.venv 已存在但缺少 Scripts\python.exe（可能是在其它系统上创建的）：删除 server\.venv 后重试。"
    }
    Invoke-Native '创建虚拟环境' $ServerDir $script:Python.File ($script:Python.Args + @('-m', 'venv', '.venv'))
}
Invoke-Native '安装后端依赖' $ServerDir $VenvPython @('-m', 'pip', 'install', '-e', '.[dev]')

Write-Step '数据库迁移与 seed'
$migrated = $false
for ($attempt = 1; $attempt -le 3 -and -not $migrated; $attempt++) {
    try {
        Invoke-Native "alembic upgrade head（第 $attempt 次）" $ServerDir $VenvPython @('-m', 'alembic', 'upgrade', 'head')
        $migrated = $true
    } catch {
        # MySQL 刚 healthy 时偶有连接被拒，稍等重试；三次都失败再报错
        if ($attempt -eq 3) { throw "数据库迁移失败：确认 MySQL 可连接且 server\.env 的 DATABASE_URL 正确（$($_.Exception.Message)）" }
        Write-Warn "迁移失败，5 秒后重试：$($_.Exception.Message)"
        Start-Sleep -Seconds 5
    }
}
Invoke-Native 'seed（超管、示例项目、系统模板、默认平台）' $ServerDir $VenvPython @('seeds\seed.py')

Write-Step '前端依赖与共享包'
Invoke-Native '安装前端依赖' $Root 'pnpm' @('install')
Invoke-Native '构建 @aicreat/shared' $Root 'pnpm' @('build:shared')

Write-Step 'zhiqiapi Key'
$apiKey = Get-EnvValue $ServerEnv 'ZHIQI_API_KEY'
if ([string]::IsNullOrEmpty($apiKey)) {
    Write-Warn 'server\.env 的 ZHIQI_API_KEY 为空 → Mock 模式（不调用 zhiqiapi，全流程可跑通）'
    Write-Info '接入真实 zhiqiapi：在 server\.env 填 ZHIQI_API_KEY 与 ZHIQI_*_DEFAULT_MODEL，然后执行 pnpm dev:restart（docs/06 第六节）'
} else {
    Write-Info 'ZHIQI_API_KEY 已填写 → 真实模式（Key 不在此打印）'
    $emptyModels = @(@('ZHIQI_TEXT_DEFAULT_MODEL', 'ZHIQI_IMAGE_DEFAULT_MODEL', 'ZHIQI_VIDEO_DEFAULT_MODEL', 'ZHIQI_GEO_DEFAULT_MODEL', 'ZHIQI_SEO_DEFAULT_MODEL') |
        Where-Object { [string]::IsNullOrEmpty((Get-EnvValue $ServerEnv $_)) })
    if ($emptyModels.Count -gt 0) {
        Write-Warn "以下默认模型为空，对应能力路由启动后会被停用，可在后台「AI 网关 → 能力路由」补填：$($emptyModels -join ', ')"
    }
}

if ($NoStart) {
    Write-Host ''
    Write-Host '初始化完成（-NoStart）。启动：pnpm dev:restart' -ForegroundColor Green
    exit 0
}

Write-Step '启动：scripts\dev-restart.ps1'
$global:LASTEXITCODE = 0
& $RestartScript
$restartCode = $LASTEXITCODE
Write-Host ''
Write-Host "默认账号:  admin / admin123（首次登录后请修改密码）" -ForegroundColor Green
Write-Host '之后重启只需 pnpm dev:restart；本脚本可重复执行（已完成的步骤不会重复改动数据）。'
if ($restartCode -ne 0) { exit $restartCode }
