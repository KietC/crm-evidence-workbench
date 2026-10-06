[CmdletBinding()]
param(
    [ValidateRange(1, 8)]
    [int]$Instance = 1,

    [switch]$SkipBuild,

    [switch]$NoProfileClone,

    [switch]$CloneLocalProfile,

    [switch]$NoMonitor,

    [ValidatePattern('^\d+$')]
    [string]$OneShotCompanyId
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'native-arguments.ps1')
if ($CloneLocalProfile -and $NoProfileClone) { throw 'Choose only one profile-clone option.' }
$AppRoot = Split-Path -Parent $PSScriptRoot
$PrimaryRuntime = Join-Path $AppRoot 'runtime'
$IsOneShot = -not [string]::IsNullOrWhiteSpace($OneShotCompanyId)
$RuntimeRoot = if ($IsOneShot) {
    Join-Path $PrimaryRuntime ("one-shot\company_{0}" -f $OneShotCompanyId)
} elseif ($Instance -eq 1) {
    $PrimaryRuntime
} else {
    Join-Path $PrimaryRuntime ("instances\instance-{0}" -f $Instance)
}
$Electron = Join-Path $AppRoot 'node_modules\.bin\electron.cmd'
$Entry = Join-Path $AppRoot 'dist\electron-main.js'
$MonitorEntry = Join-Path $AppRoot 'dist\codex-monitor.js'
$MonitorLock = Join-Path $RuntimeRoot 'codex-monitor.lock'
$ControlPort = if ($IsOneShot) { 3311 } else { 3211 + (($Instance - 1) * 10) }
$CdpPort = if ($IsOneShot) { 9434 } else { 9334 + (($Instance - 1) * 10) }

# Treat all planned collectors as one bounded workload. Explicit environment
# overrides remain available, but the defaults divide a single RAM/CPU budget
# instead of letting every instance size itself from the whole workstation.
# 全部实例共享一份 CPU/内存预算；不要让每个实例都按整机资源扩张。
$LogicalProcessors = [int]((Get-CimInstance Win32_Processor | Measure-Object NumberOfLogicalProcessors -Sum).Sum)
$TotalMemoryGB = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$InstanceCount = 1
$ParsedInstanceCount = 0
if ([int]::TryParse($env:OKKI_INSTANCE_COUNT, [ref]$ParsedInstanceCount) -and $ParsedInstanceCount -ge 1 -and $ParsedInstanceCount -le 8) {
    $InstanceCount = $ParsedInstanceCount
}
. (Join-Path $PSScriptRoot 'performance-budget.ps1')
$Budget = Get-OkkiPerformanceBudget -TotalMemoryGB $TotalMemoryGB -LogicalProcessors $LogicalProcessors -InstanceCount $InstanceCount
function Set-BoundedIntegerEnvironment {
    param([string]$Name, [int]$Maximum, [int]$Minimum = 1)
    $Current = 0
    $Raw = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not ([int]::TryParse($Raw, [ref]$Current) -and $Current -ge $Minimum -and $Current -le $Maximum)) {
        [Environment]::SetEnvironmentVariable($Name, [string]$Maximum, 'Process')
    }
}
Set-BoundedIntegerEnvironment -Name 'OKKI_API_PAGE_CONCURRENCY' -Maximum $Budget.ApiPageConcurrency
Set-BoundedIntegerEnvironment -Name 'OKKI_MAIL_DETAIL_CONCURRENCY' -Maximum $Budget.MailDetailConcurrency
Set-BoundedIntegerEnvironment -Name 'OKKI_RESOURCE_DOWNLOAD_CONCURRENCY' -Maximum $Budget.ResourceDownloadConcurrency
Set-BoundedIntegerEnvironment -Name 'OKKI_CASE_SCAN_CONCURRENCY' -Maximum $Budget.CaseScanConcurrency
Set-BoundedIntegerEnvironment -Name 'OKKI_NODE_HEAP_MB' -Maximum $Budget.NodeHeapPerInstanceMB -Minimum 4096
if (-not $env:OKKI_SETTLE_SCALE_PERCENT) { $env:OKKI_SETTLE_SCALE_PERCENT = '65' }
Set-BoundedIntegerEnvironment -Name 'UV_THREADPOOL_SIZE' -Maximum $Budget.UvThreadpoolSize

if (-not (Test-Path -LiteralPath $Electron)) {
    & (Join-Path $PSScriptRoot 'install.ps1')
}

if (-not $SkipBuild) {
    Push-Location $AppRoot
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "build failed: $LASTEXITCODE" }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $Entry)) { throw "Desktop entry is missing: $Entry" }
if (-not (Test-Path -LiteralPath $MonitorEntry)) { throw "Codex monitor entry is missing: $MonitorEntry" }

New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null

# Profile/cookie reuse is opt-in and stays local. Fresh login is the default.
# 复用本地浏览器配置与 Cookie 必须主动选择；默认创建独立配置并人工登录。
if (($Instance -gt 1 -or $IsOneShot) -and $CloneLocalProfile -and -not $NoProfileClone) {
    $SourceProfile = Join-Path $PrimaryRuntime 'electron-app-data'
    $TargetProfile = Join-Path $RuntimeRoot 'electron-app-data'
    $TargetPartitionCookies = Join-Path $TargetProfile 'Partitions\okki-crm-capture\Network\Cookies'
    $PrimaryCdp = 'http://127.0.0.1:9334'
    $PrimaryCdpListening = Get-NetTCPConnection -State Listen -LocalPort 9334 -ErrorAction SilentlyContinue
    if ((Test-Path -LiteralPath $SourceProfile) -and -not ((Test-Path -LiteralPath (Join-Path $TargetProfile 'Local State')) -and (Test-Path -LiteralPath $TargetPartitionCookies))) {
        New-Item -ItemType Directory -Path $TargetProfile -Force | Out-Null
        & robocopy.exe $SourceProfile $TargetProfile /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS /NP `
            /XF 'lockfile' 'LOCK' '*.lock' 'SingletonCookie' 'SingletonLock' 'SingletonSocket' 'Cookies' 'Cookies-journal' 'Cookies-wal' `
            /XD 'Cache' 'Code Cache' 'GPUCache' 'DawnGraphiteCache' 'DawnWebGPUCache' 'Crashpad' | Out-Null
        $RobocopyCode = $LASTEXITCODE
        if (-not $PrimaryCdpListening) {
            $Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
            if (-not $Python) { $Python = (Get-Command py.exe -ErrorAction SilentlyContinue).Source }
            if (-not $Python) { throw 'Python is required to clone the offline Chromium cookie database consistently.' }
            & $Python (Join-Path $PSScriptRoot 'clone-cookie-databases.py') $SourceProfile $TargetProfile
            if ($LASTEXITCODE -ne 0) { throw "Cookie database backup failed: $LASTEXITCODE" }
        }
        if ($RobocopyCode -gt 9) { throw "Unable to clone the primary local login profile. Robocopy exit code: $RobocopyCode" }
    }
    $CookieBootstrap = Join-Path $RuntimeRoot 'cookie-bootstrap.json'
    $CookieBootstrapCount = 0
    $CookieSourceInstance = $null
    foreach ($SourceInstance in 1..$InstanceCount) {
        if (-not $IsOneShot -and $SourceInstance -eq $Instance) { continue }
        $SourceCdpPort = 9334 + (($SourceInstance - 1) * 10)
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $SourceCdpPort -ErrorAction SilentlyContinue)) { continue }
        $BootstrapOutput = & node.exe (Join-Path $PSScriptRoot 'clone-session-cookies.mjs') "http://127.0.0.1:$SourceCdpPort" $CookieBootstrap
        if ($LASTEXITCODE -ne 0) { continue }
        try { $CookieBootstrapCount = @([IO.File]::ReadAllText($CookieBootstrap) | ConvertFrom-Json).Count } catch { $CookieBootstrapCount = 0 }
        if ($CookieBootstrapCount -gt 0) { $CookieSourceInstance = $SourceInstance; break }
    }
    if ($CookieBootstrapCount -gt 0) {
        $env:OKKI_COOKIE_BOOTSTRAP = $CookieBootstrap
        Write-Output "cookie_bootstrap_count=$CookieBootstrapCount source_instance=$CookieSourceInstance"
    } else {
        Remove-Item -LiteralPath $CookieBootstrap -Force -ErrorAction SilentlyContinue
    }
    $SourceLayout = Join-Path $PrimaryRuntime 'desktop-layout.json'
    $TargetLayout = Join-Path $RuntimeRoot 'desktop-layout.json'
    if ((Test-Path -LiteralPath $SourceLayout) -and -not (Test-Path -LiteralPath $TargetLayout)) {
        Copy-Item -LiteralPath $SourceLayout -Destination $TargetLayout
    }
}

$PortBusy = Get-NetTCPConnection -State Listen -LocalPort $ControlPort -ErrorAction SilentlyContinue
if ($PortBusy) { throw "Collector instance $Instance is already running or port $ControlPort is occupied." }

$env:OKKI_INSTANCE_ID = if ($IsOneShot) { '9' } else { [string]$Instance }
$env:OKKI_RUNTIME_ROOT = $RuntimeRoot
$env:OKKI_SHARED_RUNTIME_ROOT = if ($IsOneShot) { $RuntimeRoot } else { $PrimaryRuntime }
$env:OKKI_CAPTURE_PORT = [string]$ControlPort
$env:OKKI_CDP_PORT = [string]$CdpPort
$env:OKKI_QUEUE_COORDINATOR = if (-not $IsOneShot -and $Instance -eq 1) { '1' } else { '0' }
if ($IsOneShot) { $env:OKKI_ONE_SHOT_COMPANY_ID = $OneShotCompanyId }
else { Remove-Item Env:OKKI_ONE_SHOT_COMPANY_ID -ErrorAction SilentlyContinue }

if (-not $IsOneShot -and $Instance -eq 1 -and -not $NoMonitor) {
    $MonitorRunning = $false
    if (Test-Path -LiteralPath $MonitorLock) {
        $MonitorPid = 0
        [void][int]::TryParse((Get-Content -LiteralPath $MonitorLock -Raw).Trim(), [ref]$MonitorPid)
        if ($MonitorPid -gt 0) {
            $MonitorRunning = $null -ne (Get-Process -Id $MonitorPid -ErrorAction SilentlyContinue)
        }
    }
    if (-not $MonitorRunning) {
        Remove-Item -LiteralPath $MonitorLock -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath 'node.exe' -ArgumentList @(ConvertTo-NativePathArgument $MonitorEntry) -WorkingDirectory $AppRoot -WindowStyle Hidden
    }
}

$ElectronProcess = Start-Process -FilePath $Electron -ArgumentList @(ConvertTo-NativePathArgument $Entry) -WorkingDirectory $AppRoot -PassThru
try { $ElectronProcess.PriorityClass = 'AboveNormal' } catch { }
[pscustomobject]@{
    instance = $Instance
    one_shot = $IsOneShot
    control = "http://127.0.0.1:$ControlPort"
    cdp = "http://127.0.0.1:$CdpPort"
    runtime = $RuntimeRoot
    profile_cloned = ($Instance -gt 1 -or $IsOneShot) -and $CloneLocalProfile -and -not $NoProfileClone
    performance = "instances=$InstanceCount api_each=$env:OKKI_API_PAGE_CONCURRENCY mail_each=$env:OKKI_MAIL_DETAIL_CONCURRENCY resources_each=$env:OKKI_RESOURCE_DOWNLOAD_CONCURRENCY case_scan_each=$env:OKKI_CASE_SCAN_CONCURRENCY heap_each_mb=$env:OKKI_NODE_HEAP_MB heap_total_mb=$($Budget.TotalNodeHeapMB) reserve_gb=$($Budget.ReservedMemoryGB)"
}
