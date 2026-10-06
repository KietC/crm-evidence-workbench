[CmdletBinding()]
param([switch]$GenerateWorkflowIndex)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
$ProjectRoot = Split-Path -Parent $AppRoot
$WorkspaceRoot = Split-Path -Parent $ProjectRoot

$node = Get-Command node -ErrorAction Stop
$npm = Get-Command npm -ErrorAction Stop

Write-Host "[1/4] Node $(& $node.Source --version)"
# npm ci verifies the committed lock and never silently resolves a different tree.
# npm ci 严格使用已提交的依赖锁，不静默解析成另一棵依赖树。
Write-Host "[2/4] Install locked dependencies / 安装锁定依赖"
Push-Location $AppRoot
try {
    & $npm.Source ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed: $LASTEXITCODE" }

    Write-Host "[3/4] TypeScript build and smoke / 构建与自检"
    & $npm.Source run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed: $LASTEXITCODE" }
    & $npm.Source run smoke
    if ($LASTEXITCODE -ne 0) { throw "npm run smoke failed: $LASTEXITCODE" }
}
finally {
    Pop-Location
}

Write-Host "[4/4] Optional local workflow index / 可选本地工作流索引"
if ($GenerateWorkflowIndex) {
    $IndexTool = Join-Path $ProjectRoot 'tools\build_workflow_index.py'
    if (Test-Path -LiteralPath $IndexTool -PathType Leaf) {
        & python $IndexTool --workspace-root $WorkspaceRoot
        if ($LASTEXITCODE -ne 0) { throw "workflow index generation failed: $LASTEXITCODE" }
    } else {
        Write-Warning "未打包可选工作流索引工具，跳过：$IndexTool"
    }
}

Write-Host "Installed / 安装完成：$AppRoot"
