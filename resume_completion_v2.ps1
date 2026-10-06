# Resume only the explicitly selected case; never infer a previous customer.
# 仅续跑明确指定的案例，绝不推断或复用历史客户。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CaseRoot,
    [Parameter(Mandatory)][ValidatePattern('^\d+$')][string]$CompanyId,
    [Parameter(Mandatory)][string]$ExpectedCountsPath,
    [int]$ParseWorkers = 32,
    [int]$OcrWorkers = 24,
    [int]$RelationshipWorkers = 48,
    [string]$Python = 'python',
    [string]$Ffmpeg = $(if ($env:EVIDENCE_FFMPEG) { $env:EVIDENCE_FFMPEG } else { 'ffmpeg' }),
    [string]$Ffprobe = $(if ($env:EVIDENCE_FFPROBE) { $env:EVIDENCE_FFPROBE } else { 'ffprobe' }),
    [string]$Pdftoppm = $(if ($env:EVIDENCE_PDFTOPPM) { $env:EVIDENCE_PDFTOPPM } else { 'pdftoppm' }),
    [string]$Ghostscript = $(if ($env:EVIDENCE_GHOSTSCRIPT) { $env:EVIDENCE_GHOSTSCRIPT } else { 'gswin64c' }),
    [string]$Tesseract = $(if ($env:EVIDENCE_TESSERACT) { $env:EVIDENCE_TESSERACT } else { 'tesseract' }),
    [string]$Tessdata = $env:TESSDATA_PREFIX,
    [string]$AsrScript = $env:EVIDENCE_ASR_SCRIPT,
    [string]$AsrPython = $env:EVIDENCE_ASR_PYTHON,
    [switch]$DryRun,
    [switch]$AsJson
)

$ErrorActionPreference = 'Stop'

function Read-Json([string]$Path, [string]$Code) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Code`: $Path" }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

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
        throw 'ACTIVE_WORKER_CHECK_FAILED'
    }
    return [pscustomobject]@{ extraction = $extract; relationship = $relationship }
}

function Write-Result([hashtable]$Value) {
    if ($AsJson) { [pscustomobject]$Value | ConvertTo-Json -Depth 8 -Compress }
    else { [pscustomobject]$Value | Format-List }
}

function Invoke-NativeChecked([string]$File, [string[]]$Arguments, [int[]]$AllowedExitCodes = @(0)) {
    if ($DryRun) { return }
    & $File @Arguments
    $code = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $code) {
        throw "COMMAND_FAILED_$code`: $([System.IO.Path]::GetFileName($File))"
    }
}

if ($ParseWorkers -lt 1 -or $ParseWorkers -gt 48) { throw 'PARSE_WORKERS_OUT_OF_RANGE' }
if ($OcrWorkers -lt 1 -or $OcrWorkers -gt 24) { throw 'OCR_WORKERS_OUT_OF_RANGE' }
if ($RelationshipWorkers -lt 1 -or $RelationshipWorkers -gt 48) { throw 'RELATIONSHIP_WORKERS_OUT_OF_RANGE' }
if (-not $DryRun) { $Python = (Get-Command $Python -ErrorAction Stop).Source }

$caseFull = [System.IO.Path]::GetFullPath($CaseRoot)
$pointerPath = Join-Path $caseFull 'work\completion_v2_current.json'
$pointer = Read-Json $pointerPath 'COMPLETION_POINTER_MISSING'
$companyId = [string]$pointer.company_id
$runId = [string]$pointer.run_id
$runRoot = [System.IO.Path]::GetFullPath([string]$pointer.run_root)
if ($companyId -ne $CompanyId) { throw 'COMPANY_BINDING_MISMATCH' }
# Expected counts belong to this case and are explicit user input, not sample defaults.
# 预期数量必须属于当前案例且由使用者明确提供，不使用示例默认值。
$expected = Read-Json $ExpectedCountsPath 'EXPECTED_COUNTS_MISSING'
if ([string]$expected.schema -ne 'evidence_trail.expected_counts.v1' -or [string]$expected.company_id -ne $CompanyId) {
    throw 'EXPECTED_COUNTS_BINDING_INVALID'
}
$expectedNames = @('old_files','historical_mail_files','min_evidence_files','attachment_occurrences','mail_attachment_relations','multiparent_attachments')
foreach ($expectedName in $expectedNames) {
    if ($expected.PSObject.Properties.Name -notcontains $expectedName -or [string]$expected.$expectedName -notmatch '^\d+$') {
        throw 'EXPECTED_COUNT_INVALID'
    }
    [int64]$expected.$expectedName | Out-Null
}
if ($runId -notmatch '^\d{8}T\d{6}Z$') { throw 'RUN_ID_INVALID' }
if (-not (Test-ChildPath (Join-Path $caseFull 'work') $runRoot)) { throw 'RUN_ROOT_OUTSIDE_CASE_WORK' }
if (-not (Test-Path -LiteralPath $runRoot -PathType Container)) { throw "RUN_ROOT_MISSING: $runRoot" }

$freeze = Read-Json (Join-Path $runRoot 'baseline\freeze_manifest.json') 'FREEZE_MANIFEST_MISSING'
if ([string]$freeze.status -ne 'FROZEN' -or [string]$freeze.run_id -ne $runId -or [string]$freeze.company_id -ne $companyId) {
    throw 'FREEZE_BINDING_INVALID'
}
$oldPackage = [System.IO.Path]::GetFullPath([string]$freeze.previous_delivery)
$pipeline = Join-Path (Split-Path -Parent $PSCommandPath) 'pipeline'
$extractScript = Join-Path $pipeline 'single_customer_full_extract.py'
$relationScript = Join-Path $pipeline 'build_relation_timeline_v2.py'
$deliveryScript = Join-Path $pipeline 'build_completion_v2_delivery.py'
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
$uiManifest = Join-Path $caseFull 'manifests\ui_gap_revisit_latest.json'
$deliveryParent = Join-Path $caseFull 'deliveries\full_unredacted_local'
$deliveryStage = Join-Path $deliveryParent ('.completion_v2_' + $runId + '.stage')
$deliveryFinal = Join-Path $deliveryParent ('completion_v2_' + $runId)

$ui = Read-JsonIfPresent $uiManifest
if (-not $ui -or [string]$ui.status -ne 'PASS' -or [string]$ui.company_id -ne $companyId) {
    Write-Result ([ordered]@{
        schema='okki.single_customer.completion_v2.resume.v1'; status='BLOCKED'; stage='UI_GAP_REVISIT';
        error_code='UI_GAP_REVISIT_PASS_REQUIRED'; run_id=$runId; run_root=$runRoot; ui_gap_manifest=$uiManifest
    })
    exit 2
}

if (Test-Path -LiteralPath $deliveryFinal -PathType Container) {
    if (-not $DryRun) { Invoke-NativeChecked $Python @($deliveryScript, 'verify', '--delivery', $deliveryFinal) }
    Write-Result ([ordered]@{
        schema='okki.single_customer.completion_v2.resume.v1'; status=if($DryRun){'DRY_RUN'}else{'COMPLETE'};
        stage='FINAL_VERIFY'; run_id=$runId; run_root=$runRoot; delivery=$deliveryFinal; next_action='NONE'
    })
    exit 0
}

if (Test-Path -LiteralPath $deliveryStage -PathType Container) {
    $xlsx = Join-Path $deliveryStage ("客户${companyId}_全案例递归补全目录_v2.xlsx")
    $docx = Join-Path $deliveryStage ("客户${companyId}_全案例递归补全研究报告_v2.docx")
    $pdf = Join-Path $deliveryStage ("客户${companyId}_全案例递归补全研究报告_v2.pdf")
    $workbookReceipt = Join-Path $deliveryStage 'qa\workbook_v2_verification.json'
    $reportReceipt = Join-Path $deliveryStage 'qa\report_v2_verification.json'
    $artifactsComplete = (Test-Path -LiteralPath $xlsx -PathType Leaf) -and (Test-Path -LiteralPath $docx -PathType Leaf) -and (Test-Path -LiteralPath $pdf -PathType Leaf) -and (Test-Path -LiteralPath $workbookReceipt -PathType Leaf) -and (Test-Path -LiteralPath $reportReceipt -PathType Leaf)
    if ($artifactsComplete) {
        if (-not $DryRun) {
            Invoke-NativeChecked $Python @($deliveryScript, 'finalize', '--stage', $deliveryStage)
            Invoke-NativeChecked $Python @($deliveryScript, 'verify', '--delivery', $deliveryFinal)
        }
        Write-Result ([ordered]@{
            schema='okki.single_customer.completion_v2.resume.v1'; status=if($DryRun){'DRY_RUN'}else{'COMPLETE'};
            stage='FINALIZE_AND_VERIFY'; run_id=$runId; run_root=$runRoot; delivery=$deliveryFinal; next_action='NONE'
        })
        exit 0
    }
    Write-Result ([ordered]@{
        schema='okki.single_customer.completion_v2.resume.v1'; status=if($DryRun){'DRY_RUN'}else{'ARTIFACT_AUTHORING_REQUIRED'};
        stage='DELIVERY_STAGED'; run_id=$runId; run_root=$runRoot; delivery_stage=$deliveryStage;
        payload=(Join-Path $deliveryStage 'delivery_payload.json'); xlsx=$xlsx; docx=$docx; pdf=$pdf;
        next_action='BUILD_AND_VISUALLY_VERIFY_XLSX_DOCX_PDF_THEN_RERUN'
    })
    exit 0
}

$workers = Get-ActiveWorkerCounts $runRoot
if ($workers.extraction -gt 0 -or $workers.relationship -gt 0) {
    Write-Result ([ordered]@{
        schema='okki.single_customer.completion_v2.resume.v1'; status='WAITING_ACTIVE_WORKERS'; stage='WORKERS_RUNNING';
        run_id=$runId; run_root=$runRoot; extraction_workers=[int]$workers.extraction;
        relationship_workers=[int]$workers.relationship; next_action='RERUN_AFTER_WORKERS_EXIT'
    })
    exit 0
}

$extractVerificationPath = Join-Path $extractRoot 'verification.json'
$extractVerification = Read-JsonIfPresent $extractVerificationPath
$extractTerminal = $extractVerification -and [string]$extractVerification.status -in @('PASS','INCOMPLETE_SOURCE_GAPS') -and [int64]$extractVerification.pending -eq 0 -and [int64]$extractVerification.failed -eq 0
if (-not $extractTerminal) {
    $statePath = Join-Path $extractRoot 'state.sqlite3'
    $scopePath = Join-Path $extractRoot 'processing_scope.v2.json'
    $needPrepare = -not (Test-Path -LiteralPath $statePath -PathType Leaf) -or -not (Test-Path -LiteralPath $scopePath -PathType Leaf)
    if (-not $needPrepare) {
        $scope = Read-JsonIfPresent $scopePath
        $scopeUiHash = [string]$scope.parents.ui_gap_revisit_manifest.sha256
        $currentUiHash = (Get-FileHash -LiteralPath $uiManifest -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($scopeUiHash.ToLowerInvariant() -ne $currentUiHash) { $needPrepare = $true }
    }
    if ($needPrepare) {
        Invoke-NativeChecked $Python @($extractScript, 'prepare', '--case-root', $caseFull, '--company-id', $companyId, '--output-dir', $extractRoot)
    }
    if ($DryRun) {
        Write-Result ([ordered]@{
            schema='okki.single_customer.completion_v2.resume.v1'; status='DRY_RUN'; stage='FULL_EXTRACT_V2';
            run_id=$runId; run_root=$runRoot; output=$extractRoot; prepare_required=$needPrepare;
            parse_workers=$ParseWorkers; ocr_workers=$OcrWorkers; next_action='RUN_FULL_EXTRACT_V2_RESUME_THEN_VERIFY'
        })
        exit 0
    }
    # Optional tools are resolved by the extractor per task. Missing tools produce
    # explicit task errors/source gaps rather than silently claiming completion.
    # 可选工具由提取器按任务解析，缺失会产生明确错误/缺口而非伪装完成。
    $extractArguments = @(
        $extractScript, 'run', '--case-root', $caseFull, '--company-id', $companyId, '--output-dir', $extractRoot,
        '--parse-workers', [string]$ParseWorkers, '--ocr-workers', [string]$OcrWorkers,
        '--ffmpeg', $Ffmpeg, '--ffprobe', $Ffprobe, '--pdftoppm', $Pdftoppm,
        '--ghostscript', $Ghostscript, '--tesseract', $Tesseract, '--ocr-languages', 'chi_sim+eng'
    )
    if ($Tessdata) { $extractArguments += @('--tessdata-dir', $Tessdata) }
    if ($AsrScript) { $extractArguments += @('--asr-script', $AsrScript) }
    if ($AsrPython) { $extractArguments += @('--asr-python', $AsrPython) }
    Invoke-NativeChecked $Python $extractArguments
    Invoke-NativeChecked $Python @($extractScript, 'verify', '--case-root', $caseFull, '--company-id', $companyId, '--output-dir', $extractRoot) @(0,3)
    $extractVerification = Read-Json $extractVerificationPath 'FULL_EXTRACT_VERIFICATION_MISSING'
    $extractTerminal = [string]$extractVerification.status -in @('PASS','INCOMPLETE_SOURCE_GAPS') -and [int64]$extractVerification.pending -eq 0 -and [int64]$extractVerification.failed -eq 0
    if (-not $extractTerminal) { throw 'FULL_EXTRACT_V2_NOT_TERMINAL' }
}

$relationVerificationPath = Join-Path $relationRoot 'verification.json'
$relationVerification = Read-JsonIfPresent $relationVerificationPath
if (-not $relationVerification -or [string]$relationVerification.status -ne 'PASS') {
    if ($DryRun) {
        Write-Result ([ordered]@{
            schema='okki.single_customer.completion_v2.resume.v1'; status='DRY_RUN'; stage='RELATIONSHIP_V2';
            run_id=$runId; run_root=$runRoot; source_package=$oldPackage; output=$relationRoot;
            relationship_workers=$RelationshipWorkers; next_action='BUILD_AND_VERIFY_RELATIONSHIP_V2'
        })
        exit 0
    }
    Invoke-NativeChecked $Python @(
        $relationScript, 'build', '--case-root', $caseFull, '--company-id', $companyId,
        '--source-package', $oldPackage, '--output-dir', $relationRoot,
        '--completion-run-dir', $runRoot, '--workers', [string]$RelationshipWorkers,
        '--expected-attachment-occurrences', [string]$expected.attachment_occurrences,
        '--expected-mail-attachment-relations', [string]$expected.mail_attachment_relations,
        '--expected-multiparent-attachments', [string]$expected.multiparent_attachments
    )
    Invoke-NativeChecked $Python @($relationScript, 'verify', '--case-root', $caseFull, '--package-dir', $relationRoot)
}

if ($DryRun) {
    Write-Result ([ordered]@{
        schema='okki.single_customer.completion_v2.resume.v1'; status='DRY_RUN'; stage='DELIVERY_PREPARE';
        run_id=$runId; run_root=$runRoot; old_package=$oldPackage; full_extract_v2=$extractRoot;
        relationship_v2=$relationRoot; ui_gap_manifest=$uiManifest; delivery_stage=$deliveryStage;
        next_action='PREPARE_DELIVERY_STAGE_THEN_AUTHOR_ARTIFACTS'
    })
    exit 0
}

Invoke-NativeChecked $Python @(
    $deliveryScript, 'prepare', '--case-root', $caseFull, '--company-id', $companyId,
    '--old-package', $oldPackage, '--full-extract-v2', $extractRoot,
    '--relationship-v2', $relationRoot, '--ui-gap-manifest', $uiManifest,
    '--output-parent', $deliveryParent, '--run-id', $runId,
    '--expected-old-files', [string]$expected.old_files,
    '--expected-historical-mail-files', [string]$expected.historical_mail_files,
    '--expected-min-evidence-files', [string]$expected.min_evidence_files,
    '--expected-attachment-occurrences', [string]$expected.attachment_occurrences,
    '--expected-mail-attachment-relations', [string]$expected.mail_attachment_relations,
    '--expected-multiparent-attachments', [string]$expected.multiparent_attachments
)

Write-Result ([ordered]@{
    schema='okki.single_customer.completion_v2.resume.v1'; status='ARTIFACT_AUTHORING_REQUIRED';
    stage='DELIVERY_STAGED'; run_id=$runId; run_root=$runRoot; delivery_stage=$deliveryStage;
    payload=(Join-Path $deliveryStage 'delivery_payload.json');
    next_action='BUILD_AND_VISUALLY_VERIFY_XLSX_DOCX_PDF_THEN_RERUN'
})

