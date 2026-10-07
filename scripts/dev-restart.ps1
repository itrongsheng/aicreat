<#
.SYNOPSIS
  aicreat 本机开发一键重启（Windows）：API（uvicorn --reload）、app.worker、app.monitor_worker、管理后台（Vite）。

.DESCRIPTION
  docs/06-getting-started.md「一键启动 / 重启（Windows）」与 docs/02 §4：
  1. 结束本脚本上一次启动的四个进程（按 scripts/.dev-restart.state.json 记录的 PID + 启动时间核对，不结束其它程序）；
  2. 端口仍被其它进程占用时从候选端口中选空闲端口：API 8100 / 8101 / 8110 / 8111 / 8120，后台 5174 / 5176 / 5177 / 5178 / 5179；
  3. 同步改写 server/.env 的 PUBLIC_BASE_URL / OSS_PUBLIC_BASE_URL / ALLOWED_ORIGINS（URL 只在指向本机回环地址或为空时改写，
     不覆盖已配置的公网 / CDN 地址）与 apps/admin/vite.config.ts 的代理目标；
  4. 用 server\.venv\Scripts\python.exe 以隐藏窗口启动 uvicorn、app.worker、app.monitor_worker，用 pnpm exec vite --strictPort 启动后台；
  5. 请求 /api/v1/health 与 /admin/ 做健康检查并打印实际地址。
  隐藏窗口的进程不落日志文件：需要看日志时改为在四个前台终端分别运行 pnpm dev:server / dev:worker / dev:monitor / dev:admin。
  首次运行前先完成 server/.env、MySQL / Redis、venv、迁移与 seed，并在仓库根执行 pnpm install 与 pnpm build:shared。

.EXAMPLE
  pnpm dev:restart
  powershell -ExecutionPolicy Bypass -File scripts/dev-restart.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $Root 'server'
$AdminDir = Join-Path $Root 'apps\admin'
$SharedDist = Join-Path $Root 'packages\shared\dist'
$Python = Join-Path $ServerDir '.venv\Scripts\python.exe'
$EnvFile = Join-Path $ServerDir '.env'
$ViteConfig = Join-Path $AdminDir 'vite.config.ts'
$StateFile = Join-Path $PSScriptRoot '.dev-restart.state.json'
$ApiCandidates = @(8100, 8101, 8110, 8111, 8120)
$AdminCandidates = @(5174, 5176, 5177, 5178, 5179)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-Warn([string]$Message) { Write-Host "    [警告] $Message" -ForegroundColor Yellow }

# ------------------------------------------------------------------ 前置检查

if ($env:OS -ne 'Windows_NT') {
    throw 'dev-restart.ps1 仅用于 Windows；macOS / Linux 请在四个终端分别运行 docs/06「常用命令」中的等价命令。'
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 $Python：请先在 server/ 下创建虚拟环境并 pip install -e "".[dev]""（docs/06 第二节）。"
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "未找到 server\.env：请先 copy server\.env.example server\.env 并按 docs/06 第一节填写。"
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw '未找到 pnpm：请先 corepack enable（pnpm@9），并在仓库根执行 pnpm install。'
}
if (-not (Test-Path -LiteralPath (Join-Path $AdminDir 'node_modules'))) {
    throw '未找到 apps\admin\node_modules：请先在仓库根执行 pnpm install。'
}
if (-not (Test-Path -LiteralPath $SharedDist)) {
    throw '未找到 packages\shared\dist：本脚本不构建共享包，请先在仓库根执行 pnpm build:shared。'
}

# ------------------------------------------------------------------ 进程工具

function Get-ProcessStartTime([System.Diagnostics.Process]$Process) {
    try { return $Process.StartTime.ToUniversalTime().ToString('o') } catch { return $null }
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StateFile)) { return @() }
    try {
        $state = [System.IO.File]::ReadAllText($StateFile, $Utf8NoBom) | ConvertFrom-Json
        if ($null -eq $state -or $null -eq $state.processes) { return @() }
        return @($state.processes)
    } catch {
        Write-Warn "状态文件 $StateFile 无法解析，忽略：$($_.Exception.Message)"
        return @()
    }
}

function Stop-PreviousProcesses {
    $entries = Read-State
    if ($entries.Count -eq 0) {
        Write-Info '没有本脚本先前启动的进程记录'
        return
    }
    foreach ($entry in $entries) {
        $proc = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if ($null -eq $proc) {
            Write-Info "$($entry.name)（PID $($entry.pid)）已不在运行"
            continue
        }
        # PID 可能被系统复用：启动时间不一致说明已不是本脚本启动的进程，不结束
        $started = Get-ProcessStartTime $proc
        if ($entry.started_at -and $started -and $started -ne $entry.started_at) {
            Write-Info "$($entry.name)（PID $($entry.pid)）启动时间不符，已被其它程序复用，跳过"
            continue
        }
        Write-Info "结束 $($entry.name)（PID $($entry.pid)，含子进程）"
        # taskkill /T 结束整棵进程树（uvicorn --reload 的子进程、cmd → pnpm → node）；不用 2>&1，Windows PowerShell 5.1
        # 在 ErrorActionPreference=Stop 下会把原生命令的 stderr 当作终止错误
        Start-Process -FilePath 'taskkill.exe' -ArgumentList "/PID $($proc.Id) /T /F" -WindowStyle Hidden -Wait
    }
    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
}

function Start-Hidden([string]$Name, [string]$FilePath, [string]$Arguments, [string]$WorkingDirectory) {
    $proc = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden -PassThru
    Write-Info ("{0,-8} PID {1,-6} {2} {3}" -f $Name, $proc.Id, (Split-Path -Leaf $FilePath), $Arguments)
    return [pscustomobject]@{ name = $Name; pid = $proc.Id; started_at = (Get-ProcessStartTime $proc) }
}

# ------------------------------------------------------------------ 端口

function Test-PortFree([int]$Port) {
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    foreach ($endpoint in $listeners) {
        if ($endpoint.Port -eq $Port) { return $false }
    }
    return $true
}

function Wait-PortsReleased([int[]]$Ports, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $busy = @($Ports | Where-Object { -not (Test-PortFree $_) })
        if ($busy.Count -eq 0) { return }
        Start-Sleep -Milliseconds 300
    }
}

function Select-Port([int[]]$Candidates, [string]$Label) {
    foreach ($port in $Candidates) {
        if (Test-PortFree $port) {
            if ($port -ne $Candidates[0]) {
                Write-Warn "$Label 端口 $($Candidates[0]) 被其它程序占用，改用 $port"
            }
            return $port
        }
    }
    throw "$Label 候选端口 $($Candidates -join ' / ') 均被占用，请先结束占用端口的进程。"
}

# ------------------------------------------------------------------ 配置改写

function Test-LoopbackUrl([string]$Value) {
    $text = $Value.Trim()
    if ($text -eq '') { return $true }
    return $text -match '^https?://(127\.0\.0\.1|localhost)(:\d+)?(/.*)?$'
}

function Split-EnvValue([string]$Raw) {
    # 返回 @(值, 行尾注释)：只处理本脚本改写的三个键（URL / 逗号分隔的 origin，不含空格与 #）
    $m = [regex]::Match($Raw, '^(?<value>.*?)(?<comment>\s+#.*)?$')
    $value = $m.Groups['value'].Value.Trim()
    if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return @($value, $m.Groups['comment'].Value)
}

function Update-EnvFile([int]$ApiPort, [int]$AdminPort) {
    $text = [System.IO.File]::ReadAllText($EnvFile, $Utf8NoBom)
    $newline = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.AddRange([string[]]($text -split "\r?\n"))
    $apiBase = "http://127.0.0.1:$ApiPort"
    $origins = @("http://127.0.0.1:$AdminPort", "http://localhost:$AdminPort")
    $handled = @{}

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $m = [regex]::Match($lines[$i], '^(?<key>PUBLIC_BASE_URL|OSS_PUBLIC_BASE_URL|ALLOWED_ORIGINS)\s*=(?<raw>.*)$')
        if (-not $m.Success) { continue }
        $key = $m.Groups['key'].Value
        $handled[$key] = $true
        $parts = Split-EnvValue $m.Groups['raw'].Value
        $current = $parts[0]
        $comment = $parts[1]
        $value = $null
        if ($key -eq 'ALLOWED_ORIGINS') {
            $others = @($current -split ',' | ForEach-Object { $_.Trim() } |
                Where-Object { $_ -ne '' -and $_ -notmatch '^http://(127\.0\.0\.1|localhost):\d+$' })
            $value = (@($origins) + $others) -join ','
        } elseif (-not (Test-LoopbackUrl $current)) {
            Write-Warn "$key=$current 不是本机地址，保持不变"
        } elseif ($key -eq 'PUBLIC_BASE_URL') {
            $value = $apiBase
        } else {
            $value = "$apiBase/media"
        }
        if ($null -ne $value) { $lines[$i] = "$key=$value$comment" }
    }
    if (-not $handled.ContainsKey('PUBLIC_BASE_URL')) { $lines.Add("PUBLIC_BASE_URL=$apiBase") }
    if (-not $handled.ContainsKey('OSS_PUBLIC_BASE_URL')) { $lines.Add("OSS_PUBLIC_BASE_URL=$apiBase/media") }
    if (-not $handled.ContainsKey('ALLOWED_ORIGINS')) { $lines.Add("ALLOWED_ORIGINS=$($origins -join ',')") }

    $updated = $lines -join $newline
    if ($updated -ne $text) {
        # 不写 BOM：python-dotenv 会把 BOM 当作第一个键名的一部分
        [System.IO.File]::WriteAllText($EnvFile, $updated, $Utf8NoBom)
        Write-Info "已改写 server\.env：PUBLIC_BASE_URL / OSS_PUBLIC_BASE_URL → $apiBase，ALLOWED_ORIGINS → $($origins -join ',')"
    } else {
        Write-Info 'server\.env 无需改写'
    }
}

function Update-ViteConfig([int]$ApiPort) {
    $text = [System.IO.File]::ReadAllText($ViteConfig, $Utf8NoBom)
    $pattern = '(?<head>target:\s*["''])http://(127\.0\.0\.1|localhost):\d+(?<tail>["''])'
    $updated = [regex]::Replace($text, $pattern, "`${head}http://127.0.0.1:$ApiPort`${tail}")
    if ($updated -ne $text) {
        [System.IO.File]::WriteAllText($ViteConfig, $updated, $Utf8NoBom)
        Write-Info "已改写 apps\admin\vite.config.ts：/api、/media 代理 → http://127.0.0.1:$ApiPort"
    } else {
        Write-Info 'apps\admin\vite.config.ts 无需改写'
    }
}

# ------------------------------------------------------------------ 健康检查

function Invoke-Probe([string]$Url) {
    # 直接使用 HttpWebRequest 并关闭代理：Windows PowerShell 5.1 的 Invoke-WebRequest 会走系统代理，可能拦截本机地址
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Proxy = $null
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $response = $null
    try {
        $response = [System.Net.HttpWebResponse]$request.GetResponse()
    } catch [System.Net.WebException] {
        if ($null -eq $_.Exception.Response) { return $null }
        $response = [System.Net.HttpWebResponse]$_.Exception.Response
    }
    try {
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $reader.Close()
        return [pscustomobject]@{ Status = [int]$response.StatusCode; Body = $body }
    } finally {
        $response.Close()
    }
}

function Wait-Probe([string]$Url, [int]$TimeoutSeconds, [scriptblock]$Accept) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        $last = Invoke-Probe $Url
        if ($null -ne $last -and (& $Accept $last)) { return $last }
        Start-Sleep -Seconds 1
    }
    return $last
}

# ------------------------------------------------------------------ 主流程

Write-Step '结束本脚本上次启动的进程'
Stop-PreviousProcesses
Wait-PortsReleased -Ports @($ApiCandidates[0], $AdminCandidates[0]) -TimeoutSeconds 10

Write-Step '选择端口'
$apiPort = Select-Port $ApiCandidates 'API'
$adminPort = Select-Port $AdminCandidates '管理后台'
Write-Info "API $apiPort，管理后台 $adminPort"

Write-Step '同步端口配置'
Update-EnvFile -ApiPort $apiPort -AdminPort $adminPort
Update-ViteConfig -ApiPort $apiPort

Write-Step '启动进程（隐藏窗口，不落日志文件）'
$started = @()
$started += Start-Hidden 'api' $Python "-m uvicorn app.main:app --reload --host 127.0.0.1 --port $apiPort" $ServerDir
$started += Start-Hidden 'worker' $Python '-m app.worker' $ServerDir
$started += Start-Hidden 'monitor' $Python '-m app.monitor_worker' $ServerDir
$started += Start-Hidden 'admin' $env:ComSpec "/d /c pnpm exec vite --port $adminPort --strictPort" $AdminDir
$state = [pscustomobject]@{
    root       = $Root
    api_port   = $apiPort
    admin_port = $adminPort
    updated_at = (Get-Date).ToUniversalTime().ToString('o')
    processes  = $started
}
[System.IO.File]::WriteAllText($StateFile, ($state | ConvertTo-Json -Depth 4), $Utf8NoBom)

Write-Step '健康检查'
$apiUrl = "http://127.0.0.1:$apiPort"
$adminUrl = "http://localhost:$adminPort/admin/"
$failed = $false

$health = Wait-Probe "$apiUrl/api/v1/health" 60 { param($r) $r.Status -eq 200 -or $r.Status -eq 503 }
if ($null -eq $health) {
    Write-Warn "API 60 秒内无响应：$apiUrl/api/v1/health（改用 pnpm dev:server 前台运行查看日志）"
    $failed = $true
} else {
    # worker 心跳在启动后数秒内写入：status 由 degraded 变为 ok
    $settled = Wait-Probe "$apiUrl/api/v1/health" 30 {
        param($r)
        try { return ($r.Body | ConvertFrom-Json).data.status -eq 'ok' } catch { return $false }
    }
    if ($null -ne $settled) { $health = $settled }
    $data = $null
    try { $data = ($health.Body | ConvertFrom-Json).data } catch { $data = $null }
    if ($null -ne $data) {
        Write-Info ("API      HTTP {0} status={1} db={2} redis={3} zhiqi_mode={4} worker.alive={5} monitor_worker.alive={6}" -f `
            $health.Status, $data.status, $data.db, $data.redis, $data.zhiqi_mode, $data.workers.worker.alive, $data.workers.monitor_worker.alive)
        if ($data.status -ne 'ok') { Write-Warn 'health 未达到 ok：检查 MySQL / Redis 与 worker 进程（前台运行 pnpm dev:worker / dev:monitor 查看日志）' }
        foreach ($warning in @($data.warnings)) { if ($warning) { Write-Warn $warning } }
    } else {
        Write-Warn "API 返回 HTTP $($health.Status)"
        $failed = $true
    }
}

$admin = Wait-Probe $adminUrl 60 { param($r) $r.Status -eq 200 }
if ($null -ne $admin -and $admin.Status -eq 200) {
    Write-Info "后台     HTTP 200 $adminUrl"
} else {
    $code = if ($null -eq $admin) { '无响应' } else { "HTTP $($admin.Status)" }
    Write-Warn "管理后台 $code：$adminUrl（改用 pnpm dev:admin 前台运行查看日志）"
    $failed = $true
}

Write-Host ''
Write-Host "API:       $apiUrl" -ForegroundColor Green
Write-Host "管理后台:  $adminUrl" -ForegroundColor Green
if ($apiPort -ne $ApiCandidates[0]) {
    Write-Host "冒烟脚本:  cd server; .venv\Scripts\python.exe scripts\integration_smoke.py --base-url $apiUrl" -ForegroundColor Green
}
Write-Host '再次运行 pnpm dev:restart 会先结束以上进程。'
if ($failed) { exit 1 }
