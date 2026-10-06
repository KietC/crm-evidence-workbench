[CmdletBinding()]
param([string]$Python = 'python')

# Hash-pinned archive tools live under this application, not the global interpreter.
# 哈希固定的归档依赖只安装在本应用目录，不改写系统 Python 环境。
$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command $Python -ErrorAction Stop).Source
$Target = Join-Path $AppRoot 'runtime\python-packages'
New-Item -ItemType Directory -Force -Path $Target | Out-Null
& $Python -m pip install --disable-pip-version-check --require-hashes --upgrade --target $Target -r (Join-Path $AppRoot 'requirements-archive.txt')
if ($LASTEXITCODE -ne 0) { throw "archive dependency installation failed: $LASTEXITCODE" }
& $Python -c "import sys, importlib.metadata; sys.path.insert(0, sys.argv[1]); import warcio; print(importlib.metadata.version('warcio'))" $Target
if ($LASTEXITCODE -ne 0) { throw "warcio import verification failed: $LASTEXITCODE" }
