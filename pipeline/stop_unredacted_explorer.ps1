# EN: Local-only stop unredacted explorer utility; preserve input evidence and keep source content out of logs.
# 中文：本地辅助工具；保留输入证据，不把源内容写入外部日志。
[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 18765
)

$ErrorActionPreference = 'Stop'
$statePath = Join-Path (Join-Path $env:LOCALAPPDATA 'OKKIUnredactedExplorer') "explorer-$Port.json"
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    Write-Output "NOT_RUNNING port=$Port"
    exit 0
}

$state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
$candidateIds = [Collections.Generic.HashSet[int]]::new()
if ($state.pid) { [void]$candidateIds.Add([int]$state.pid) }
if ($state.launcher_pid) { [void]$candidateIds.Add([int]$state.launcher_pid) }
$listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') }
foreach ($listener in $listeners) { [void]$candidateIds.Add([int]$listener.OwningProcess) }

$stopped = [Collections.Generic.List[int]]::new()
foreach ($candidateId in $candidateIds) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$candidateId" -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    $commandLine = [string]$process.CommandLine
    if ($commandLine -notlike "*$($state.server)*" -or $commandLine -notlike "*--port $Port*") {
        throw "PID $candidateId does not match the registered explorer command; refusing to stop it."
    }
    Stop-Process -Id $candidateId -Force
    $stopped.Add($candidateId)
}
Remove-Item -LiteralPath $statePath -Force
Start-Sleep -Milliseconds 300
$remaining = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') }
if ($remaining) { throw "Explorer listener still owns port $Port after stop." }
if ($stopped.Count -eq 0) {
    Write-Output "NOT_RUNNING port=$Port"
} else {
    Write-Output "PASS stopped PID=$($stopped -join ',') port=$Port"
}
