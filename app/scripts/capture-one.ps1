[CmdletBinding(PositionalBinding = $false)]
param(
    [Alias('company-id')]
    [ValidatePattern('^\d+$')]
    [string]$CompanyId,

    [Alias('no-auto-next')]
    [switch]$NoAutoNext,

    [Alias('resume-existing')]
    [switch]$ResumeExisting,

    [Alias('revisit-ui-gaps')]
    [switch]$RevisitUiGaps,

    [switch]$SkipBuild,

    [switch]$NoProfileClone,

    [switch]$CloneLocalProfile,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = 'Stop'
$ControlUrl = 'http://127.0.0.1:3311'
$StartScript = Join-Path $PSScriptRoot 'start.ps1'

# PowerShell normally uses -Name, while the documented one-shot CLI also
# accepts conventional GNU-style --long-name arguments.
# PowerShell 使用 -Name，本入口同时接受 GNU 风格参数；客户身份必须显式传入。
for ($i = 0; $i -lt @($CliArgs).Count; $i++) {
    $token = $CliArgs[$i]
    switch -Regex ($token) {
        '^--company-id=(\d+)$' { $CompanyId = $Matches[1]; continue }
        '^--company-id$' {
            if ($i + 1 -ge $CliArgs.Count -or $CliArgs[$i + 1] -notmatch '^\d+$') {
                throw '--company-id requires a numeric value.'
            }
            $i += 1
            $CompanyId = $CliArgs[$i]
            continue
        }
        '^--revisit-ui-gaps$' { $RevisitUiGaps = $true; continue }
        '^--no-auto-next$' { $NoAutoNext = $true; continue }
        '^--resume-existing$' { $ResumeExisting = $true; continue }
        '^--skip-build$' { $SkipBuild = $true; continue }
        '^--no-profile-clone$' { $NoProfileClone = $true; continue }
        '^--clone-local-profile$' { $CloneLocalProfile = $true; continue }
        '^\d+$' {
            if ($CompanyId) { throw "Unexpected positional company id: $token" }
            $CompanyId = $token
            continue
        }
        default { throw "Unknown argument: $token" }
    }
}
if (-not $CompanyId -or $CompanyId -notmatch '^\d+$') {
    throw 'A numeric company id is required. Use -CompanyId or --company-id.'
}

if ($RevisitUiGaps) {
    if (-not $NoAutoNext) {
        throw '--revisit-ui-gaps requires --no-auto-next.'
    }
    if ($ResumeExisting) {
        throw '--revisit-ui-gaps always creates a new independent session; do not combine it with --resume-existing.'
    }
}

if (Get-NetTCPConnection -State Listen -LocalPort 3311 -ErrorAction SilentlyContinue) {
    throw 'one-shot control port 3311 is already occupied; stop that one-shot instance before starting another.'
}

$startArgs = @{
    OneShotCompanyId = $CompanyId
    NoMonitor = $true
}
if ($SkipBuild) { $startArgs.SkipBuild = $true }
if ($NoProfileClone) { $startArgs.NoProfileClone = $true }
if ($CloneLocalProfile) { $startArgs.CloneLocalProfile = $true }
$launchOutput = @(& $StartScript @startArgs)
$launch = $launchOutput | Where-Object { $_.PSObject.Properties.Name -contains 'runtime' } | Select-Object -Last 1
if (-not $launch) { throw 'one-shot launcher did not return its runtime descriptor.' }

$deadline = [DateTimeOffset]::UtcNow.AddSeconds(60)
$healthy = $false
while ([DateTimeOffset]::UtcNow -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Method Get -Uri "$ControlUrl/healthz" -TimeoutSec 2
        if ($health.status -eq 'ok') { $healthy = $true; break }
    }
    catch { Start-Sleep -Milliseconds 300 }
}
if (-not $healthy) { throw 'one-shot local server did not become healthy within 60 seconds.' }

# The HTTP server becomes healthy before Electron has finished attaching and
# showing its embedded CRM view.  Starting capture in that small window races
# the initial loadURL() and Electron reports ERR_ABORTED.  Wait for the desktop
# process to publish its fresh ready marker before opening the bound customer.
# 健康端点不代表桌面就绪；等待新鲜的桌面标记后再打开绑定客户，避免 ERR_ABORTED。
$desktopReadyFile = Join-Path $launch.runtime 'desktop-ready.json'
$desktopDeadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
while ([DateTimeOffset]::UtcNow -lt $desktopDeadline) {
    if (Test-Path -LiteralPath $desktopReadyFile) { break }
    Start-Sleep -Milliseconds 300
}
if (-not (Test-Path -LiteralPath $desktopReadyFile)) {
    throw 'one-shot desktop did not become ready within 90 seconds; capture was not started.'
}

$payload = @{
    resume_existing = [bool]$ResumeExisting
    revisit_ui_gaps = [bool]$RevisitUiGaps
} | ConvertTo-Json -Compress
try {
    $capture = Invoke-RestMethod -Method Post -Uri "$ControlUrl/api/capture/start" -ContentType 'application/json' -Body $payload -TimeoutSec 30
    [pscustomobject]@{
        one_shot = $true
        no_auto_next = $true
        accepted = [bool]$capture.accepted
        resume_existing = [bool]$capture.resume_existing
        revisit_ui_gaps = [bool]$capture.revisit_ui_gaps
        control = $ControlUrl
        runtime = $launch.runtime
    }
}
catch {
    $message = $_.ErrorDetails.Message
    if (-not $message) { $message = $_.Exception.Message }
    [pscustomobject]@{
        one_shot = $true
        no_auto_next = $true
        accepted = $false
        revisit_ui_gaps = [bool]$RevisitUiGaps
        action_required = 'Complete login in the opened local collector, then press Start once.'
        local_error = $message
        control = $ControlUrl
        runtime = $launch.runtime
    }
}

