# Run synthetic checks only; never start the collector or infer a customer.
# 仅运行合成检查，不启动采集器或推断客户。
[CmdletBinding()]
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonCommand = (Get-Command $Python -ErrorAction Stop).Source
$npmCommand = (Get-Command npm -ErrorAction Stop).Source
Push-Location $repoRoot
try {
    & $pythonCommand scripts/check.py
    if ($LASTEXITCODE -ne 0) { throw 'PYTHON_CHECK_FAILED' }
    & $npmCommand run check
    if ($LASTEXITCODE -ne 0) { throw 'ROOT_NPM_CHECK_FAILED' }
    foreach ($scriptName in @('check', 'build', 'test', 'smoke')) {
        & $npmCommand --prefix app run $scriptName
        if ($LASTEXITCODE -ne 0) { throw "APP_CHECK_FAILED_$scriptName" }
    }
} finally { Pop-Location }
