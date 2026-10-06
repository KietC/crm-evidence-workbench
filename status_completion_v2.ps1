# Read-only metadata status for one explicitly bound case.
# 仅查看明确绑定的单案例元数据状态，不执行采集或业务写入。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CaseRoot,
    [Parameter(Mandatory)][ValidatePattern('^\d+$')][string]$CompanyId,
    [string]$Python = 'python',
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'

function Read-JsonIfPresent([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    return $null
}

function Test-ChildPath([string]$Parent, [string]$Child) {
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-ActiveWorkerCounts([string]$RunRoot) {
    $extract = 0
    $relationship = 0
    try {
        foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
            $line = [string]$process.CommandLine
            if (-not $line -or $line.IndexOf($RunRoot, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            if ($line -match 'single_customer_full_extract\.py\s+run') { $extract++ }
            if ($line -match 'build_relation_timeline_v2\.py\s+build') { $relationship++ }
        }
    } catch {
        # Remain useful when WMI/CIM is unavailable.
    }
    return [pscustomobject]@{ extraction = $extract; relationship = $relationship }
}

function Read-TaskCounts([string]$Database, [string]$Python) {
    if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) { return $null }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $null }
    $code = @'
import json, sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
try:
    rows = dict(db.execute("SELECT state,COUNT(*) FROM tasks WHERE active=1 GROUP BY state"))
    files = int(db.execute("SELECT COUNT(*) FROM files WHERE active=1").fetchone()[0])
    print(json.dumps({"files": files, "tasks": sum(rows.values()), "states": rows}, sort_keys=True))
finally:
    db.close()
'@
    try {
        $raw = & $Python -c $code $Database 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) { return ($raw | ConvertFrom-Json) }
    } catch {
        return $null
    }
    return $null
}

$caseFull = [System.IO.Path]::GetFullPath($CaseRoot)
$pointerPath = Join-Path $caseFull 'work\completion_v2_current.json'
if (-not (Test-Path -LiteralPath $pointerPath -PathType Leaf)) {
    throw "completion-v2 pointer not found: $pointerPath"
}
$pointer = Read-JsonIfPresent $pointerPath
$runRoot = [System.IO.Path]::GetFullPath([string]$pointer.run_root)
$workRoot = Join-Path $caseFull 'work'
if ([string]$pointer.company_id -ne $CompanyId -or -not (Test-ChildPath $workRoot $runRoot)) {
    throw 'completion-v2 pointer binding is invalid'
}

$freeze = Read-JsonIfPresent (Join-Path $runRoot 'baseline\freeze_manifest.json')
$ui = Read-JsonIfPresent (Join-Path $caseFull 'manifests\ui_gap_revisit_latest.json')
$extractRoot = Join-Path $runRoot 'derived\full_extract_v2'
$relationFallback = Join-Path $runRoot 'derived\relationship_v2'
$relationOverride = if ($pointer.PSObject.Properties.Name -contains 'relationship_v2') { [string]$pointer.relationship_v2 } else { '' }
$relationRoot = if (
    -not [string]::IsNullOrWhiteSpace($relationOverride) -and
    [System.IO.Path]::IsPathFullyQualified($relationOverride) -and
    (Test-Path -LiteralPath $relationOverride -PathType Container)
) {
    [System.IO.Path]::GetFullPath($relationOverride)
} else {
    $relationFallback
}
$extractVerification = Read-JsonIfPresent (Join-Path $extractRoot 'verification.json')
$relationVerification = Read-JsonIfPresent (Join-Path $relationRoot 'verification.json')
$python = (Get-Command $Python -ErrorAction Stop).Source
$taskCounts = Read-TaskCounts (Join-Path $extractRoot 'state.sqlite3') $python
$workers = Get-ActiveWorkerCounts $runRoot

$deliveryParent = Join-Path $caseFull 'deliveries\full_unredacted_local'
$deliveryStage = Join-Path $deliveryParent ('.completion_v2_' + [string]$pointer.run_id + '.stage')
$deliveryFinal = Join-Path $deliveryParent ('completion_v2_' + [string]$pointer.run_id)
$deliveryVerification = Read-JsonIfPresent (Join-Path $deliveryFinal 'verification.json')
$prepareManifest = Read-JsonIfPresent (Join-Path $deliveryStage 'prepare_manifest.json')

$extractStatus = if ($extractVerification) { [string]$extractVerification.status } elseif ($workers.extraction -gt 0) { 'RUNNING' } elseif ($taskCounts) { 'PREPARED_OR_INTERRUPTED' } else { 'NOT_STARTED' }
$relationStatus = if ($relationVerification) { [string]$relationVerification.status } elseif ($workers.relationship -gt 0) { 'RUNNING' } elseif (Test-Path -LiteralPath $relationRoot) { 'PREPARED_OR_INTERRUPTED' } else { 'NOT_STARTED' }
$deliveryStatus = if ($deliveryVerification) { [string]$deliveryVerification.status } elseif ($prepareManifest) { [string]$prepareManifest.status } else { 'NOT_STARTED' }

$nextAction = if ($deliveryVerification) {
    'COMPLETE_VERIFY_ONLY'
} elseif ($prepareManifest) {
    $company = [string]$pointer.company_id
    $xlsx = Join-Path $deliveryStage ("客户${company}_全案例递归补全目录_v2.xlsx")
    $docx = Join-Path $deliveryStage ("客户${company}_全案例递归补全研究报告_v2.docx")
    $pdf = Join-Path $deliveryStage ("客户${company}_全案例递归补全研究报告_v2.pdf")
    $receipts = (Test-Path -LiteralPath (Join-Path $deliveryStage 'qa\workbook_v2_verification.json')) -and (Test-Path -LiteralPath (Join-Path $deliveryStage 'qa\report_v2_verification.json'))
    if ((Test-Path -LiteralPath $xlsx) -and (Test-Path -LiteralPath $docx) -and (Test-Path -LiteralPath $pdf) -and $receipts) { 'FINALIZE_AND_VERIFY' } else { 'AUTHOR_XLSX_DOCX_PDF_AND_VISUALLY_VERIFY' }
} elseif ($workers.extraction -gt 0 -or $workers.relationship -gt 0) {
    'WAIT_FOR_ACTIVE_WORKERS'
} elseif (-not $extractVerification -or [string]$extractVerification.status -notin @('PASS', 'INCOMPLETE_SOURCE_GAPS')) {
    'RESUME_FULL_EXTRACT_V2'
} elseif (-not $relationVerification -or [string]$relationVerification.status -ne 'PASS') {
    'BUILD_RELATIONSHIP_V2'
} elseif (-not $ui -or [string]$ui.status -ne 'PASS') {
    'RECOVER_UI_GAPS'
} else {
    'PREPARE_DELIVERY_STAGE'
}

$states = if ($taskCounts) { $taskCounts.states } else { $null }
$result = [ordered]@{
    schema = 'okki.single_customer.completion_v2.status.v1'
    company_id = [string]$pointer.company_id
    run_id = [string]$pointer.run_id
    run_root = $runRoot
    freeze_status = if ($freeze) { [string]$freeze.status } else { 'MISSING' }
    frozen_files = if ($freeze) { [int64]$freeze.counts.files } else { 0 }
    frozen_bytes = if ($freeze) { [int64]$freeze.counts.bytes } else { 0 }
    ui_gap_status = if ($ui) { [string]$ui.status } else { 'MISSING' }
    ui_gap_session_id = if ($ui) { [string]$ui.session_id } else { '' }
    extraction_status = $extractStatus
    extraction_files = if ($extractVerification) { [int64]$extractVerification.evidence_files } elseif ($taskCounts) { [int64]$taskCounts.files } else { 0 }
    extraction_tasks = if ($extractVerification) { [int64]$extractVerification.tasks } elseif ($taskCounts) { [int64]$taskCounts.tasks } else { 0 }
    extraction_pending = if ($extractVerification) { [int64]$extractVerification.pending } elseif ($states) { [int64]$states.pending + [int64]$states.running } else { 0 }
    extraction_failed = if ($extractVerification) { [int64]$extractVerification.failed } elseif ($states) { [int64]$states.failed } else { 0 }
    extraction_source_gaps = if ($extractVerification) { [int64]$extractVerification.source_gaps } elseif ($states) { [int64]$states.source_gap } else { 0 }
    extraction_workers = [int]$workers.extraction
    full_extract_v2 = $extractRoot
    relationship_status = $relationStatus
    relationship_workers = [int]$workers.relationship
    relationship_v2 = $relationRoot
    delivery_status = $deliveryStatus
    accessible_data = if ($deliveryVerification) { [string]$deliveryVerification.status } else { 'NOT_EVALUATED' }
    absolute_completeness = if ($deliveryVerification) { [string]$deliveryVerification.absolute_completeness_status } else { 'NOT_EVALUATED' }
    delivery_stage = $deliveryStage
    delivery_final = $deliveryFinal
    next_action = $nextAction
}

if ($AsJson) {
    [pscustomobject]$result | ConvertTo-Json -Depth 6 -Compress
} else {
    [pscustomobject]$result | Format-List
}

