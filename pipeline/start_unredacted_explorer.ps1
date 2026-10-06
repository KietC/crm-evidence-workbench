# EN: Local-only start unredacted explorer utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Database,
    [ValidateRange(1024, 65535)]
    [int]$Port = 18765,
    [switch]$Open,
    [string]$Python = $env:CRM_PYTHON
)

$ErrorActionPreference = 'Stop'
# EN: Activate the documented virtual environment or pass its interpreter explicitly.
# 中文：先激活文档所述虚拟环境，或明确指定解释器。
$python = $Python
if (-not $python) {
    $command = Get-Command python -ErrorAction Stop
    $python = $command.Source
}
$server = Join-Path $PSScriptRoot 'unredacted_explorer_server.py'
$databasePath = (Resolve-Path -LiteralPath $Database).Path

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python runtime missing: $python" }
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { throw "Explorer server missing: $server" }

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is already in use." }

$stateRoot = Join-Path $env:LOCALAPPDATA 'OKKIUnredactedExplorer'
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
$statePath = Join-Path $stateRoot "explorer-$Port.json"
$stdoutPath = Join-Path $stateRoot "explorer-$Port.stdout.log"
$stderrPath = Join-Path $stateRoot "explorer-$Port.stderr.log"

$arguments = @('"' + $server + '"', '--db', '"' + $databasePath + '"', '--port', [string]$Port)
if ($Open) { $arguments += '--open' }
$process = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

@{
    schema = 'okki.single_customer.unredacted_explorer_process.v1'
    status = 'STARTING'
    pid = $process.Id
    launcher_pid = $process.Id
    port = $Port
    database = $databasePath
    python = $python
    server = $server
    started_at_utc = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Milliseconds 250
    $process.Refresh()
    if ($process.HasExited) { throw "Explorer exited early. See $stderrPath" }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/meta" -TimeoutSec 2
        if ($health.integrity -eq 'ok') { break }
    } catch {
        if ((Get-Date) -ge $deadline) { throw "Explorer did not become ready within 90 seconds." }
    }
} while ((Get-Date) -lt $deadline)

$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
    Select-Object -First 1
if (-not $listener) { throw "Explorer passed health check but no localhost listener owns port $Port." }
$workerPid = [int]$listener.OwningProcess

@{
    schema = 'okki.single_customer.unredacted_explorer_process.v1'
    status = 'READY'
    pid = $workerPid
    launcher_pid = $process.Id
    port = $Port
    database = $databasePath
    python = $python
    server = $server
    started_at_utc = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8

Write-Output "PASS http://127.0.0.1:$Port/ PID=$workerPid LAUNCHER=$($process.Id)"
